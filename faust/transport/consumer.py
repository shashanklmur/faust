"""Consumer

The Consumer is responsible for:

   - Holds reference to the transport that created it

   - ... and the app via ``self.transport.app``.

   - Has a callback that usually points back to ``Conductor.on_message``.

   - Receives messages and calls the callback for every message received.

   - Keeps track of the message and it's acked/unacked status.

   - The Conductor forwards the message to all Streams that subscribes
     to the topic the message was sent to.

       + Messages are reference counted, and the Conductor increases
         the reference count to the number of subscribed streams.

       + Stream.__aiter__ is set up in a way such that when what is iterating
         over the stream is finished with the message, a finally: block will
         decrease the reference count by one.

       + When the reference count for a message hits zero, the stream will
         call ``Consumer.ack(message)``, which will mark that tp+offset
         combination as "commitable"

       + If all the streams share the same key_type/value_type,
         the conductor will only deserialize the payload once.

   - Commits the offset at an interval

      + The Consumer has a background thread that periodically commits the
        offset.

      - If the consumer marked an offset as committable this thread
        will advance the comitted offset.

      + To find the offset that it can safely advance to the commit thread
        will traverse the _acked mapping of TP to list of acked offsets, by
        finding a range of consecutive acked offsets (see note in
        _new_offset).

"""
import abc
import asyncio
import gc
import typing
from collections import defaultdict
from time import monotonic
from typing import (
    Any,
    AsyncIterator,
    ClassVar,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    MutableMapping,
    MutableSet,
    Optional,
    Set,
    Tuple,
    Type,
    cast,
)
from weakref import WeakSet

from mode import Service, ServiceT, flight_recorder, get_logger
from mode.threads import MethodQueue, QueueServiceThread
from mode.utils.futures import notify
from mode.utils.locks import Event
from mode.utils.times import Seconds
from faust.exceptions import ProducerSendError
from faust.types import AppT, ConsumerMessage, Message, TP
from faust.types.transports import (
    ConsumerCallback,
    ConsumerT,
    PartitionsAssignedCallback,
    PartitionsRevokedCallback,
    TPorTopicSet,
    TransportT,
)
from faust.utils import terminal
from faust.utils.functional import consecutive_numbers

from .utils import TopicBuffer, TopicIndexMap

if typing.TYPE_CHECKING:  # pragma: no cover
    from faust.app import App
else:
    class App: ...  # noqa: E701

__all__ = ['Consumer', 'Fetcher']

# These flags are used for Service.diag, tracking what the consumer
# service is currently doing.
CONSUMER_FETCHING = 'FETCHING'
CONSUMER_PARTITIONS_REVOKED = 'PARTITIONS_REVOKED'
CONSUMER_PARTITIONS_ASSIGNED = 'PARTITIONS_ASSIGNED'
CONSUMER_COMMITTING = 'COMMITTING'
CONSUMER_SEEKING = 'SEEKING'
CONSUMER_WAIT_EMPTY = 'WAIT_EMPTY'

logger = get_logger(__name__)

RecordMap = Mapping[TP, List[Any]]


def ensure_TP(tp: Any) -> TP:
    return tp if isinstance(tp, TP) else TP(tp.topic, tp.partition)


def ensure_TPset(tps: Iterable[Any]) -> Set[TP]:
    return {ensure_TP(tp) for tp in tps}


class Fetcher(Service):
    app: AppT

    logger = logger
    _drainer: Optional[asyncio.Future] = None

    def __init__(self, app: AppT, **kwargs: Any) -> None:
        self.app = app
        super().__init__(**kwargs)

    async def on_stop(self) -> None:
        if self._drainer is not None and not self._drainer.done():
            self._drainer.cancel()
            while True:
                try:
                    await asyncio.wait_for(self._drainer, timeout=1.0)
                except StopIteration:
                    # Task is cancelled right before coro stops.
                    pass
                except asyncio.CancelledError:
                    break
                except asyncio.TimeoutError:
                    self.log.warn('Fetcher is ignoring cancel or slow :(')
                else:
                    break

    @Service.task
    async def _fetcher(self) -> None:
        try:
            consumer = cast(Consumer, self.app.consumer)
            self._drainer = asyncio.ensure_future(
                consumer._drain_messages(self),
                loop=self.loop,
            )
            await self._drainer
        except asyncio.CancelledError:
            pass
        finally:
            self.set_shutdown()


class Consumer(Service, ConsumerT):
    """Base Consumer."""

    app: AppT

    logger = logger

    #: Tuple of exception types that may be raised when the
    #: underlying consumer driver is stopped.
    consumer_stopped_errors: ClassVar[Tuple[Type[BaseException], ...]] = ()

    # Mapping of TP to list of acked offsets.
    _acked: MutableMapping[TP, List[int]]

    #: Fast lookup to see if tp+offset was acked.
    _acked_index: MutableMapping[TP, Set[int]]

    #: Keeps track of the currently read offset in each TP
    _read_offset: MutableMapping[TP, Optional[int]]

    #: Keeps track of the currently commited offset in each TP.
    _committed_offset: MutableMapping[TP, Optional[int]]

    #: The consumer.wait_empty() method will set this to be notified
    #: when something acks a message.
    _waiting_for_ack: Optional[asyncio.Future] = None

    #: Used by .commit to ensure only one thread is comitting at a time.
    #: Other thread starting to commit while a commit is already active,
    #: will wait for the original request to finish, and do nothing.
    _commit_fut: Optional[asyncio.Future] = None

    #: Set of unacked messages: that is messages that we started processing
    #: and that we MUST attempt to complete processing of, before
    #: shutting down or resuming a rebalance.
    _unacked_messages: MutableSet[Message]

    #: Time of last record batch received.
    #: Set only when not set, and reset by commit() so actually
    #: tracks how long it ago it was since we received a record that
    #: was never committed.
    _last_batch: Optional[float]

    #: Time of when the consumer was started.
    _time_start: float

    # How often to poll and track log end offsets.
    _end_offset_monitor_interval: float

    _commit_every: Optional[int]
    _n_acked: int = 0

    _active_partitions: Optional[Set[TP]]
    _paused_partitions: Set[TP]

    flow_active: bool = True
    can_resume_flow: Event

    def __init__(self,
                 transport: TransportT,
                 callback: ConsumerCallback,
                 on_partitions_revoked: PartitionsRevokedCallback,
                 on_partitions_assigned: PartitionsAssignedCallback,
                 *,
                 commit_interval: float = None,
                 commit_livelock_soft_timeout: float = None,
                 loop: asyncio.AbstractEventLoop = None,
                 **kwargs: Any) -> None:
        assert callback is not None
        self.transport = transport
        self.app = self.transport.app
        self.callback = callback
        self._on_message_in = self.app.sensors.on_message_in
        self._on_partitions_revoked = on_partitions_revoked
        self._on_partitions_assigned = on_partitions_assigned
        self._commit_every = self.app.conf.broker_commit_every
        self.commit_interval = (
            commit_interval or self.app.conf.broker_commit_interval)
        self.commit_livelock_soft_timeout = (
            commit_livelock_soft_timeout or
            self.app.conf.broker_commit_livelock_soft_timeout)
        self._acked = defaultdict(list)
        self._acked_index = defaultdict(set)
        self._read_offset = defaultdict(lambda: None)
        self._committed_offset = defaultdict(lambda: None)
        self._unacked_messages = WeakSet()
        self._waiting_for_ack = None
        self._time_start = monotonic()
        self._last_batch = None
        self._end_offset_monitor_interval = self.commit_interval * 2
        self.randomly_assigned_topics = set()
        self.can_resume_flow = Event()
        self._reset_state()
        super().__init__(loop=loop or self.transport.loop, **kwargs)

    def _reset_state(self) -> None:
        self._active_partitions = None
        self._paused_partitions = set()
        self.can_resume_flow.clear()
        self.flow_active = True
        self._last_batch = None
        self._time_start = monotonic()

    async def on_restart(self) -> None:
        self._reset_state()
        self.on_init()

    def _get_active_partitions(self) -> Set[TP]:
        tps = self._active_partitions
        if tps is None:
            return self._set_active_tps(self.assignment())
        assert all(isinstance(x, TP) for x in tps)
        return tps

    def _set_active_tps(self, tps: Set[TP]) -> Set[TP]:
        xtps = self._active_partitions = ensure_TPset(tps)  # copy
        xtps.difference_update(self._paused_partitions)
        return xtps

    def _records_to_topic_index(
            self,
            records: RecordMap,
            active_partitions: Set[TP]) -> TopicIndexMap:
        return TopicBuffer.map_from_records(records)

    @abc.abstractmethod
    async def _commit(
            self,
            offsets: Mapping[TP, int]) -> bool:  # pragma: no cover
        ...

    async def perform_seek(self) -> None:
        read_offset = self._read_offset
        _committed_offsets = await self.seek_to_committed()
        committed_offsets = {
            ensure_TP(tp): offset
            for tp, offset in _committed_offsets.items()
            if offset is not None
        }
        read_offset.update({
            tp: offset if offset else None
            for tp, offset in committed_offsets.items()
        })
        self._committed_offset.update(committed_offsets)

    @abc.abstractmethod
    async def seek_to_committed(self) -> Mapping[TP, int]:
        ...

    async def seek(self, partition: TP, offset: int) -> None:
        self.log.dev('SEEK %r -> %r', partition, offset)
        # reset livelock detection
        self._last_batch = None
        # set new read offset so we will reread messages
        self._read_offset[ensure_TP(partition)] = offset if offset else None
        self._seek(partition, offset)

    @abc.abstractmethod
    async def _seek(self, partition: TP, offset: int) -> None:
        ...

    def stop_flow(self) -> None:
        self.flow_active = False
        self.can_resume_flow.clear()

    def resume_flow(self) -> None:
        self.flow_active = True
        self.can_resume_flow.set()

    def pause_partitions(self, tps: Iterable[TP]) -> None:
        tpset = ensure_TPset(tps)
        self._get_active_partitions().difference_update(tpset)
        self._paused_partitions.update(tpset)

    def resume_partitions(self, tps: Iterable[TP]) -> None:
        tpset = ensure_TPset(tps)
        self._get_active_partitions().update(tps)
        self._paused_partitions.difference_update(tpset)

    @abc.abstractmethod
    def _new_topicpartition(
            self, topic: str, partition: int) -> TP:  # pragma: no cover
        ...

    def _is_changelog_tp(self, tp: TP) -> bool:
        return tp.topic in self.app.tables.changelog_topics

    @Service.transitions_to(CONSUMER_PARTITIONS_REVOKED)
    async def on_partitions_revoked(self, revoked: Set[TP]) -> None:
        self.app.on_rebalance_start()
        # see comment in on_partitions_assigned
        # remove revoked partitions from active + paused tps.
        if self._active_partitions is not None:
            self._active_partitions.difference_update(revoked)
        self._paused_partitions.difference_update(revoked)
        await self._on_partitions_revoked(revoked)

    @Service.transitions_to(CONSUMER_PARTITIONS_ASSIGNED)
    async def on_partitions_assigned(self, assigned: Set[TP]) -> None:
        # remove recently revoked tps from set of paused tps.
        self._paused_partitions.intersection_update(assigned)
        # cache set of assigned partitions
        self._set_active_tps(assigned)
        # start callback chain of assigned callbacks.
        #   need to copy set at this point, since we cannot have
        #   the callbacks mutate our active list.
        self._last_batch = None
        await self._on_partitions_assigned(assigned)

    @abc.abstractmethod
    async def _getmany(self,
                       active_partitions: Set[TP],
                       timeout: float) -> RecordMap:
        ...

    async def getmany(self,
                      timeout: float) -> AsyncIterator[Tuple[TP, Message]]:
        if not self.flow_active:
            await self.wait(self.can_resume_flow)
        # Implementation for the Fetcher service.
        active_partitions = self._get_active_partitions()
        _next = next

        records: RecordMap = {}
        if active_partitions:
            # Fetch records only if active partitions to avoid the risk of
            # fetching all partitions in the beginning when none of the
            # partitions is paused/resumed.
            records = await self._getmany(
                active_partitions=active_partitions,
                timeout=timeout,
            )
        else:
            # We should still release to the event loop
            await self.sleep(1)
            if self.should_stop:
                return

        # records' contain mapping from TP to list of messages.
        # if there are two agents, consuming from topics t1 and t2,
        # normal order of iteration would be to process each
        # tp in the dict:
        #    for tp. messages in records.items():
        #        for message in messages:
        #           yield tp, message
        #
        # The problem with this, is if we have prefetched 16k records
        # for one partition, the other partitions won't even start processing
        # before those 16k records are completed.
        #
        # So we try round-robin between the tps instead:
        #
        #    iterators: Dict[TP, Iterator] = {
        #        tp: iter(messages)
        #        for tp, messages in records.items()
        #    }
        #    while iterators:
        #        for tp, messages in iterators.items():
        #            yield tp, next(messages)
        #            # remove from iterators if empty.
        #
        # The problem with this implementation is that
        # the records mapping is ordered by TP, so records.keys()
        # will look like this:
        #
        #  TP(topic='bar', partition=0)
        #  TP(topic='bar', partition=1)
        #  TP(topic='bar', partition=2)
        #  TP(topic='bar', partition=3)
        #  TP(topic='foo', partition=0)
        #  TP(topic='foo', partition=1)
        #  TP(topic='foo', partition=2)
        #  TP(topic='foo', partition=3)
        #
        # If there are 100 partitions for each topic,
        # it will process 100 items in the first topic, then 100 items
        # in the other topic, but even worse if partition counts
        # vary greatly, t1 has 1000 partitions and t2
        # has 1 partition, then t2 will end up being starved most of the time.
        #
        # We solve this by going round-robin through each topic.
        topic_index = self._records_to_topic_index(records, active_partitions)
        to_remove: Set[str] = set()
        sentinel = object()

        to_message = self._to_message  # localize

        while topic_index:
            if not self.flow_active:
                break
            for topic in to_remove:
                topic_index.pop(topic, None)
            for topic, messages in topic_index.items():
                if not self.flow_active:
                    break
                item = _next(messages, sentinel)
                if item is sentinel:
                    # this topic is now empty,
                    # but we cannot remove from dict while iterating over it,
                    # so move that to the outer loop.
                    to_remove.add(topic)
                    continue
                tp, record = item  # type: ignore
                if tp in active_partitions:
                    highwater_mark = self.highwater(tp)
                    self.app.monitor.track_tp_end_offset(tp, highwater_mark)
                    # convert timestamp to seconds from int milliseconds.
                    yield tp, to_message(tp, record)

    @abc.abstractmethod
    def _to_message(self, tp: TP, record: Any) -> ConsumerMessage:
        ...

    def track_message(self, message: Message) -> None:
        # add to set of pending messages that must be acked for graceful
        # shutdown.  This is called by transport.Conductor,
        # before delivering messages to streams.
        self._unacked_messages.add(message)
        # call sensors
        self._on_message_in(message.tp, message.offset, message)

    def ack(self, message: Message) -> bool:
        if not message.acked:
            message.acked = True
            tp = message.tp
            offset = message.offset
            if self.app.topics.acks_enabled_for(message.topic):
                committed = self._committed_offset[tp]
                try:
                    if committed is None or offset > committed:
                        acked_index = self._acked_index[tp]
                        if offset not in acked_index:
                            self._unacked_messages.discard(message)
                            acked_index.add(offset)
                            acked_for_tp = self._acked[tp]
                            acked_for_tp.append(offset)
                            self._n_acked += 1
                            return True
                finally:
                    notify(self._waiting_for_ack)
        return False

    async def _wait_for_ack(self, timeout: float) -> None:
        # arm future so that `ack()` can wake us up
        self._waiting_for_ack = asyncio.Future(loop=self.loop)
        try:
            # wait for `ack()` to wake us up
            await asyncio.wait_for(
                self._waiting_for_ack, loop=self.loop, timeout=1)
        except (asyncio.TimeoutError,
                asyncio.CancelledError):  # pragma: no cover
            pass
        finally:
            self._waiting_for_ack = None

    @Service.transitions_to(CONSUMER_WAIT_EMPTY)
    async def wait_empty(self) -> None:
        """Wait for all messages that started processing to be acked."""
        wait_count = 0
        while not self.should_stop and self._unacked_messages:
            wait_count += 1
            if not wait_count % 100_000:  # pragma: no cover
                remaining = [(m.refcount, m) for m in self._unacked_messages]
                self.log.warn(f'Waiting for {remaining}')
            self.log.dev('STILL WAITING FOR ALL STREAMS TO FINISH')
            self.log.dev('WAITING FOR %r EVENTS', len(self._unacked_messages))
            gc.collect()
            await self.commit()
            if not self._unacked_messages:
                break
            await self._wait_for_ack(timeout=1)

        self.log.dev('COMMITTING AGAIN AFTER STREAMS DONE')
        await self.commit()

    async def on_stop(self) -> None:
        if self.app.conf.stream_wait_empty:
            await self.wait_empty()
        else:
            await self.commit()
        self._last_batch = None

    @Service.task
    async def _commit_handler(self) -> None:
        await self.sleep(self.commit_interval)
        while not self.should_stop:
            await self.commit()
            await self.sleep(self.commit_interval)

    @Service.task
    async def _commit_livelock_detector(self) -> None:  # pragma: no cover
        soft_timeout = self.commit_livelock_soft_timeout
        interval: float = self.commit_interval * 2.5
        await self.sleep(interval)
        while not self.should_stop:
            if self._last_batch is not None:
                s_since_batch = monotonic() - self._last_batch
                if s_since_batch > soft_timeout:
                    self.log.warn(
                        'Possible livelock: COMMIT OFFSET NOT ADVANCING')
            await self.sleep(interval)

    async def commit(
            self, topics: TPorTopicSet = None) -> bool:  # pragma: no cover
        """Maybe commit the offset for all or specific topics.

        Arguments:
            topics: Set containing topics and/or TopicPartitions to commit.
        """
        if await self.maybe_wait_for_commit_to_finish():
            # original commit finished, return False as we did not commit
            return False

        self._commit_fut = asyncio.Future(loop=self.loop)
        try:
            return await self.force_commit(topics)
        finally:
            # set commit_fut to None so that next call will commit.
            fut, self._commit_fut = self._commit_fut, None
            # notify followers that the commit is done.
            if fut is not None and not fut.done():
                fut.set_result(None)

    async def maybe_wait_for_commit_to_finish(self) -> bool:
        # Only one coroutine allowed to commit at a time,
        # and other coroutines should wait for the original commit to finish
        # then do nothing.
        if self._commit_fut is not None:
            # something is already committing so wait for that future.
            try:
                await self._commit_fut
            except asyncio.CancelledError:
                # if future is cancelled we have to start new commit
                pass
            else:
                return True
        return False

    @Service.transitions_to(CONSUMER_COMMITTING)
    async def force_commit(self, topics: TPorTopicSet = None) -> bool:
        sensor_state = self.app.sensors.on_commit_initiated(self)

        # Go over the ack list in each topic/partition
        commit_tps = list(self._filter_tps_with_pending_acks(topics))
        did_commit = await self._commit_tps(commit_tps)

        self.app.sensors.on_commit_completed(self, sensor_state)
        return did_commit

    async def _commit_tps(self, tps: Iterable[TP]) -> bool:
        commit_offsets = self._filter_committable_offsets(tps)
        if commit_offsets:
            try:
                # send all messages attached to the new offset
                await self._handle_attached(commit_offsets)
            except ProducerSendError as exc:
                await self.crash(exc)
            else:
                return await self._commit_offsets(commit_offsets)
        return False

    def _filter_committable_offsets(self, tps: Iterable[TP]) -> Dict[TP, int]:
        commit_offsets = {}
        for tp in tps:
            # Find the latest offset we can commit in this tp
            offset = self._new_offset(tp)
            # check if we can commit to this offset
            if offset is not None and self._should_commit(tp, offset):
                commit_offsets[tp] = offset
        return commit_offsets

    async def _handle_attached(self, commit_offsets: Mapping[TP, int]) -> None:
        for tp, offset in commit_offsets.items():
            app = cast(App, self.app)
            attachments = app._attachments
            producer = app.producer
            # Start publishing the messages and return a list of pending
            # futures.
            pending = await attachments.publish_for_tp_offset(tp, offset)
            # then we wait for either
            #  1) all the attached messages to be published, or
            #  2) the producer crashing
            #
            # If the producer crashes we will not be able to send any messages
            # and it only crashes when there's an irrecoverable error.
            #
            # If we cannot commit it means the events will be processed again,
            # so conforms to at-least-once semantics.
            if pending:
                await producer.wait_many(pending)

    async def _commit_offsets(self, offsets: Mapping[TP, int]) -> bool:
        table = terminal.logtable(
            [(str(tp), str(offset))
             for tp, offset in offsets.items()],
            title='Commit Offsets',
            headers=['TP', 'Offset'],
        )
        self.log.dev('COMMITTING OFFSETS:\n%s', table)
        assignment = self.assignment()
        commitable_offsets: Dict[TP, int] = {}
        revoked: Dict[TP, int] = {}
        for tp, offset in offsets.items():
            if tp in assignment:
                commitable_offsets[tp] = offset
            else:
                revoked[tp] = offset
        if revoked:
            self.log.info(
                'Discarded commit for revoked partitions that '
                'will be eventually processed again: %r',
                revoked,
            )
        if not commitable_offsets:
            return False
        with flight_recorder(self.log, timeout=300.0) as on_timeout:
            on_timeout.info('+consumer.commit()')
            await self._commit(commitable_offsets)
            on_timeout.info('-consumer.commit()')
        self._committed_offset.update(commitable_offsets)
        self.app.monitor.on_tp_commit(commitable_offsets)
        self._last_batch = None
        return True

    def _filter_tps_with_pending_acks(
            self, topics: TPorTopicSet = None) -> Iterator[TP]:
        return (tp for tp in self._acked
                if topics is None or tp in topics or tp.topic in topics)

    def _should_commit(self, tp: TP, offset: int) -> bool:
        committed = self._committed_offset[tp]
        return committed is None or bool(offset) and offset > committed

    def _new_offset(self, tp: TP) -> Optional[int]:
        # get the new offset for this tp, by going through
        # its list of acked messages.
        acked = self._acked[tp]

        # We iterate over it until we find a gap
        # then return the offset before that.
        # For example if acked[tp] is:
        #   1 2 3 4 5 6 7 8 9
        # the return value will be: 9
        # If acked[tp] is:
        #  34 35 36 40 41 42 43 44
        #          ^--- gap
        # the return value will be: 36
        if acked:
            acked.sort()
            # Note: acked is always kept sorted.
            # find first list of consecutive numbers
            batch = next(consecutive_numbers(acked))
            # remove them from the list to clean up.
            acked[:len(batch)] = []
            self._acked_index[tp].difference_update(batch)
            # return the highest commit offset
            return batch[-1]
        return None

    async def on_task_error(self, exc: BaseException) -> None:
        await self.commit()

    async def _drain_messages(
            self, fetcher: ServiceT) -> None:  # pragma: no cover
        # This is the background thread started by Fetcher, used to
        # constantly read messages using Consumer.getmany.
        # It takes Fetcher as argument, because we must be able to
        # stop it using `await Fetcher.stop()`.
        callback = self.callback
        getmany = self.getmany
        consumer_should_stop = self._stopped.is_set
        fetcher_should_stop = fetcher._stopped.is_set

        get_read_offset = self._read_offset.__getitem__
        set_read_offset = self._read_offset.__setitem__
        flag_consumer_fetching = CONSUMER_FETCHING
        set_flag = self.diag.set_flag
        unset_flag = self.diag.unset_flag
        commit_every = self._commit_every

        try:
            while not (consumer_should_stop() or fetcher_should_stop()):
                set_flag(flag_consumer_fetching)
                ait = cast(AsyncIterator, getmany(timeout=5.0))
                # Sleeping because sometimes getmany is called in a loop
                # never releasing to the event loop
                await self.sleep(0)
                if not self.should_stop:
                    async for tp, message in ait:
                        offset = message.offset
                        r_offset = get_read_offset(tp)
                        if r_offset is None or offset > r_offset:
                            if commit_every is not None:
                                if self._n_acked >= commit_every:
                                    self._n_acked = 0
                                    await self.commit()
                            await callback(message)
                            set_read_offset(tp, offset)
                        else:
                            self.log.dev('DROPPED MESSAGE ROFF %r: k=%r v=%r',
                                         offset, message.key, message.value)
                    unset_flag(flag_consumer_fetching)

        except self.consumer_stopped_errors:
            if self.transport.app.should_stop:
                # we're already stopping so ignore
                self.log.info('Broker stopped consumer, shutting down...')
                return
            raise
        except asyncio.CancelledError:
            if self.transport.app.should_stop:
                # we're already stopping so ignore
                self.log.info('Consumer shutting down for user cancel.')
                return
            raise
        except Exception as exc:
            self.log.exception('Drain messages raised: %r', exc)
            raise
        finally:
            unset_flag(flag_consumer_fetching)

    def close(self) -> None:
        ...

    @property
    def unacked(self) -> Set[Message]:
        return cast(Set[Message], self._unacked_messages)


class ConsumerThread(QueueServiceThread):
    app: AppT
    consumer: 'ThreadDelegateConsumer'
    transport: TransportT

    def __init__(self, consumer: ConsumerT, **kwargs: Any) -> None:
        self.consumer = consumer
        self.transport = self.consumer.transport
        self.app = self.transport.app
        super().__init__(**kwargs)

    @abc.abstractmethod
    async def subscribe(self, topics: Iterable[str]) -> None:
        ...

    @abc.abstractmethod
    async def seek_to_committed(self) -> Mapping[TP, int]:
        ...

    @abc.abstractmethod
    async def commit(self, tps: Mapping[TP, int]) -> bool:
        ...

    @abc.abstractmethod
    async def position(self, tp: TP) -> Optional[int]:
        ...

    @abc.abstractmethod
    async def seek_to_beginning(self, *partitions: TP) -> None:
        ...

    @abc.abstractmethod
    async def seek_wait(self, partitions: Mapping[TP, int]) -> None:
        ...

    @abc.abstractmethod
    def seek(self, partition: TP, offset: int) -> None:
        ...

    @abc.abstractmethod
    def assignment(self) -> Set[TP]:
        ...

    @abc.abstractmethod
    def highwater(self, tp: TP) -> int:
        ...

    @abc.abstractmethod
    def topic_partitions(self, topic: str) -> Optional[int]:
        ...

    @abc.abstractmethod
    async def earliest_offsets(self,
                               *partitions: TP) -> MutableMapping[TP, int]:
        ...

    @abc.abstractmethod
    async def highwaters(self, *partitions: TP) -> MutableMapping[TP, int]:
        ...

    @abc.abstractmethod
    async def getmany(self,
                      active_partitions: Set[TP],
                      timeout: float) -> RecordMap:
        ...

    @abc.abstractmethod
    async def create_topic(self,
                           topic: str,
                           partitions: int,
                           replication: int,
                           *,
                           config: Mapping[str, Any] = None,
                           timeout: Seconds = 30.0,
                           retention: Seconds = None,
                           compacting: bool = None,
                           deleting: bool = None,
                           ensure_created: bool = False) -> None:
        ...

    async def on_partitions_revoked(
            self, revoked: Set[TP]) -> None:
        await self.consumer.threadsafe_partitions_revoked(
            self.thread_loop, revoked)

    async def on_partitions_assigned(
            self, assigned: Set[TP]) -> None:
        await self.consumer.threadsafe_partitions_assigned(
            self.thread_loop, assigned)


class ThreadDelegateConsumer(Consumer):

    _thread: ConsumerThread

    #: Main thread method queue.
    #: The consumer is running in a separate thread, and so we send
    #: requests to it via a queue.
    #: Sometimes the thread needs to call code owned by the main thread,
    #: such as App.on_partitions_revoked, and in that case the thread
    #: uses this method queue.
    _method_queue: MethodQueue

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._method_queue = MethodQueue(loop=self.loop, beacon=self.beacon)
        self.add_dependency(self._method_queue)
        self._thread = self._new_consumer_thread()
        self.add_dependency(self._thread)

    @abc.abstractmethod
    def _new_consumer_thread(self) -> ConsumerThread:
        ...

    async def threadsafe_partitions_revoked(
            self,
            receiver_loop: asyncio.AbstractEventLoop,
            revoked: Set[TP]) -> None:
        promise = await self._method_queue.call(
            receiver_loop.create_future(),
            self.on_partitions_revoked,
            revoked,
        )
        # wait for main-thread to finish processing request
        await promise

    async def threadsafe_partitions_assigned(
            self,
            receiver_loop: asyncio.AbstractEventLoop,
            assigned: Set[TP]) -> None:
        promise = await self._method_queue.call(
            receiver_loop.create_future(),
            self.on_partitions_assigned,
            assigned,
        )
        # wait for main-thread to finish processing request
        await promise

    async def _getmany(self,
                       active_partitions: Set[TP],
                       timeout: float) -> RecordMap:
        return await self._thread.getmany(active_partitions, timeout)

    async def subscribe(self, topics: Iterable[str]) -> None:
        await self._thread.subscribe(topics=topics)

    async def seek_to_committed(self) -> Mapping[TP, int]:
        return await self._thread.seek_to_committed()

    async def position(self, tp: TP) -> Optional[int]:
        return await self._thread.position(tp)

    async def seek_wait(self, partitions: Mapping[TP, int]) -> None:
        return await self._thread.seek_wait(partitions)

    async def _seek(self, partition: TP, offset: int) -> None:
        self._thread.seek(partition, offset)

    def assignment(self) -> Set[TP]:
        return self._thread.assignment()

    def highwater(self, tp: TP) -> int:
        return self._thread.highwater(tp)

    def topic_partitions(self, topic: str) -> Optional[int]:
        return self._thread.topic_partitions(topic)

    async def earliest_offsets(self,
                               *partitions: TP) -> MutableMapping[TP, int]:
        return await self._thread.earliest_offsets(*partitions)

    async def highwaters(self, *partitions: TP) -> MutableMapping[TP, int]:
        return await self._thread.highwaters(*partitions)

    async def _commit(self, offsets: Mapping[TP, int]) -> bool:
        return await self._thread.commit(offsets)

    def close(self) -> None:
        self._thread.close()
