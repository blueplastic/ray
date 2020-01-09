import logging

from collections import defaultdict

import ray
import ray.cloudpickle as cloudpickle

# This string should be identical to the name of the signal sent upon
# detecting that an actor died.
# This constant is also used in NodeManager::PublishActorStateTransition()
# in node_manager.cc
ACTOR_DIED_STR = "ACTOR_DIED_SIGNAL"

logger = logging.getLogger(__name__)


class Signal:
    """Base class for Ray signals."""
    pass


class ErrorSignal(Signal):
    """Signal raised if an exception happens in a task or actor method."""

    def __init__(self, error):
        self.error = error


class ActorDiedSignal(Signal):
    """Signal raised if an exception happens in a task or actor method."""

    def __init__(self):
        pass


def _get_task_id(source):
    """Return the task id associated to the generic source of the signal.

    Args:
        source: source of the signal, it can be either an object id returned
            by a task, a task id, or an actor handle.

    Returns:
        - If source is an object id, return id of task which creted object.
        - If source is an actor handle, return id of actor's task creator.
        - If source is a task id, return same task id.
    """
    if type(source) is ray.actor.ActorHandle:
        return source._actor_id
    else:
        if type(source) is ray.TaskID:
            return source
        else:
            return ray._raylet.compute_task_id(source)


def send(signal):
    """Send signal.

    The signal has a unique identifier that is computed from (1) the id
    of the actor or task sending this signal (i.e., the actor or task calling
    this function), and (2) an index that is incremented every time this
    source sends a signal. This index starts from 1.

    Args:
        signal: Signal to be sent.
    """
    if ray.worker.global_worker.actor_id.is_nil():
        source_key = ray.worker.global_worker.current_task_id.hex()
    else:
        source_key = ray.worker.global_worker.actor_id.hex()

    encoded_signal = ray.utils.binary_to_hex(cloudpickle.dumps(signal))
    ray.worker.global_worker.redis_client.execute_command(
        "XADD " + source_key + " * signal " + encoded_signal)


def receive(sources, timeout=None):
    """Get all outstanding signals from sources.

    A source can be either (1) an object ID returned by the task (we want
    to receive signals from), or (2) an actor handle.

    When invoked by the same entity E (where E can be an actor, task or
    driver), for each source S in sources, this function returns all signals
    generated by S since the last receive() was invoked by E on S. If this is
    the first call on S, this function returns all past signals generated by S
    so far. Note that different actors, tasks or drivers that call receive()
    on the same source S will get independent copies of the signals generated
    by S.

    Args:
        sources: List of sources from which the caller waits for signals.
            A source is either an object ID returned by a task (in this case
            the object ID is used to identify that task), or an actor handle.
            If the user passes the IDs of multiple objects returned by the
            same task, this function returns a copy of the signals generated
            by that task for each object ID.
        timeout: Maximum time (in seconds) this function waits to get a signal
            from a source in sources. If None, the timeout is infinite.

    Returns:
        A list of pairs (S, sig), where S is a source in the sources argument,
            and sig is a signal generated by S since the last time receive()
            was called on S. Thus, for each S in sources, the return list can
            contain zero or multiple entries.
    """

    # If None, initialize the timeout to a huge value (i.e., over 30,000 years
    # in this case) to "approximate" infinity.
    if timeout is None:
        timeout = 10**12

    if timeout < 0:
        raise ValueError("The 'timeout' argument cannot be less than 0.")

    if not hasattr(ray.worker.global_worker, "signal_counters"):
        ray.worker.global_worker.signal_counters = defaultdict(lambda: b"0")

    signal_counters = ray.worker.global_worker.signal_counters

    # Map the ID of each source task to the source itself.
    task_id_to_sources = defaultdict(lambda: [])
    for s in sources:
        task_id_to_sources[_get_task_id(s).hex()].append(s)

    if timeout < 1e-3:
        logger.warning("Timeout too small. Using 1ms minimum")
        timeout = 1e-3

    timeout_ms = int(1000 * timeout)

    # Construct the redis query.
    query = "XREAD BLOCK "
    # redis expects ms.
    query += str(timeout_ms)
    query += " STREAMS "
    query += " ".join(task_id_to_sources)
    query += " "
    query += " ".join([
        ray.utils.decode(signal_counters[ray.utils.hex_to_binary(task_id)])
        for task_id in task_id_to_sources
    ])

    answers = ray.worker.global_worker.redis_client.execute_command(query)
    if not answers:
        return []

    results = []
    # Decoding is a little bit involved. Iterate through all the answers:
    for i, answer in enumerate(answers):
        # Make sure the answer corresponds to a source, s, in sources.
        task_id = ray.utils.decode(answer[0])
        task_source_list = task_id_to_sources[task_id]
        # The list of results for source s is stored in answer[1]
        for r in answer[1]:
            for s in task_source_list:
                if r[1][1].decode("ascii") == ACTOR_DIED_STR:
                    results.append((s, ActorDiedSignal()))
                else:
                    # Now it gets tricky: r[0] is the redis internal sequence
                    # id
                    signal_counters[ray.utils.hex_to_binary(task_id)] = r[0]
                    # r[1] contains a list with elements (key, value), in our
                    # case we only have one key "signal" and the value is the
                    # signal.
                    signal = cloudpickle.loads(
                        ray.utils.hex_to_binary(r[1][1]))
                    results.append((s, signal))

    return results


def forget(sources):
    """Ignore all previous signals associated with each source S in sources.

    The index of the next expected signal from S is set to the index of
    the last signal that S sent plus 1. This means that the next receive()
    on S will only get the signals generated after this function was invoked.

    Args:
        sources: list of sources whose past signals are forgotten.
    """
    # Just read all signals sent by all sources so far.
    # This will results in ignoring these signals.
    receive(sources, timeout=0)


def reset():
    """
    Reset the worker state associated with any signals that this worker
    has received so far.

    If the worker calls receive() on a source next, it will get all the
    signals generated by that source starting with index = 1.
    """
    if hasattr(ray.worker.global_worker, "signal_counters"):
        ray.worker.global_worker.signal_counters = defaultdict(lambda: b"0")
