import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import cycle
from typing import Dict, List

from dask import bag
from dask.distributed import Future, get_client, Variable, wait

from cicada2.engine.actions import run_actions, combine_action_data
from cicada2.engine.asserts import run_asserts
from cicada2.shared.logs import get_logger
from cicada2.engine.state import combine_data_by_key, create_item_name
from cicada2.shared.asserts import get_remaining_asserts
from cicada2.shared.types import (
    Action,
    ActionsData,
    Assert,
    Statuses,
    TestConfig,
    TestSummary,
)


LOGGER = get_logger("testing")


def action_has_asserts(action: Action):
    return action.get("asserts", []) != []


def actions_have_asserts(actions: List[Action]):
    return any(action_has_asserts(action) for action in actions)


def get_default_cycles(actions: List[Action], asserts: List[Assert]) -> int:
    """
    Determine number of default cycles for test given actions and asserts in it

    * If there are asserts, by default run unlimited
    * If there are no asserts but actions, run only once
    * Otherwise, the test has no actions or asserts so do not run

    Args:
        actions: list of actions in test
        asserts: list of asserts in test

    Returns:
        Default number of cycles the test should have
    """
    if asserts or actions_have_asserts(actions):
        return -1
    elif actions:
        return 1

    return 0


def continue_running(
    actions: List[Action],
    asserts: List[Assert],
    remaining_cycles: int,
    actions_data: ActionsData,
    assert_statuses: Statuses,
) -> bool:
    """
    Determines if the test should continue running

    * Stop if no asserts, no action asserts, and remaining cycles == 0
    * Stop if has asserts or action asserts and remaining cycles == 0
    * Stop if has asserts or action asserts, unlimited cycles, no remaining asserts or action asserts

    * Keep going if has no remaining asserts or action asserts and remaining cycles > 0

    Args:
        asserts: List of asserts test has
        remaining_cycles: Number of remaining cycles in test
        assert_statuses: Status of asserts in list

    Returns:
        Whether to continue running or not
    """
    return (
        asserts == [] and not actions_have_asserts(actions) and remaining_cycles != 0
    ) or (
        (asserts != [] or actions_have_asserts(actions))
        and remaining_cycles != 0
        and (
            get_remaining_asserts(asserts, assert_statuses) != []
            or any(
                get_remaining_asserts(
                    action.get("asserts", []),
                    actions_data.get(action["name"], {}).get("asserts", {}),
                )
                != []
                for action in actions
            )
        )
    )


def run_actions_parallel(
    actions: List[Action],
    state: defaultdict,
    test_name: str,
    hostnames: List[str],
    seconds_between_actions: float,
) -> ActionsData:
    """
    Runs each action in provided list on each of the provided hosts. For example, if two actions are provided and there
    are two hosts, each action will be run twice (once on each host)

    Args:
        actions: List of actions to run
        state: Initial state of test to provide to actions
        test_name: Name of test
        hostnames: List of host addresses
        seconds_between_actions: Seconds to wait on host before running the next action

    Returns:
        ActionsData generated by running actions in parallel
    """
    actions_task = (
        bag.from_sequence(hostnames)
        .map(
            lambda hostname: run_actions(
                actions, {**state}, hostname, seconds_between_actions
            )
        )
        .fold(combine_action_data, initial=state[test_name].get("actions", {}))
    )

    return actions_task.compute()


def run_actions_series(
    actions: List[Action],
    state: defaultdict,
    test_name: str,
    hostnames: List[str],
    seconds_between_actions: float,
) -> ActionsData:
    """
    Runs each action distributed into each host. For example, If there are two hosts and two actions, each action will
    be run once, one action per host

    Args:
        actions: List of actions to run
        state: Initial state of test to provide to actions
        test_name: Name of test
        hostnames: List of host addresses
        seconds_between_actions: Seconds to wait on host before running the next action

    Returns:
        ActionsData generated by running actions in series
    """
    hostname_actions_map: Dict[str, List[Action]] = {}

    for hostname_action in zip(cycle(hostnames), actions):
        hostname = hostname_action[0]
        action = hostname_action[1]

        if hostname not in hostname_actions_map:
            hostname_actions_map[hostname] = [action]
        else:
            hostname_actions_map[hostname] += [action]

    actions_task = (
        bag.from_sequence(hostname_actions_map)
        .map(
            lambda h_name: run_actions(
                hostname_actions_map[h_name], {**state}, h_name, seconds_between_actions
            )
        )
        .fold(combine_action_data, initial=state[test_name].get("actions", {}))
    )

    return actions_task.compute()


def run_asserts_parallel(
    asserts: List[Assert],
    state: defaultdict,
    test_name: str,
    hostnames: List[str],
    seconds_between_asserts: float,
) -> Statuses:
    """
    Runs each assert in provided list on each of the provided hosts. For example, if two asserts are provided and there
    are two hosts, each assert will be run twice (once on each host)

    Args:
        asserts: List of asserts to run
        state: Initial state of test to provide to asserts
        test_name: Name of test
        hostnames: List of host addresses
        seconds_between_asserts: Seconds to wait on host before running the next assert

    Returns:
        Status of each assert after being run
    """
    asserts_task = (
        bag.from_sequence(hostnames)
        .map(
            lambda hostname: run_asserts(
                get_remaining_asserts(asserts, state[test_name].get("asserts", {})),
                {**state},
                hostname,
                seconds_between_asserts,
            )
        )
        .fold(combine_data_by_key, initial=state[test_name].get("asserts", {}))
    )

    return asserts_task.compute()


def run_asserts_series(
    asserts: List[Assert],
    state: defaultdict,
    test_name: str,
    hostnames: List[str],
    seconds_between_asserts: float,
) -> Statuses:
    """
    Runs each assert distributed into each host. For example, If there are two hosts and two asserts, each assert will
    be run once, one assert per host

    Args:
        asserts: List of asserts to run
        state: Initial state of test to provide to asserts
        test_name: Name of test
        hostnames: List of host addresses
        seconds_between_asserts: Seconds to wait on host before running the next assert

    Returns:
        Status of each assert after being run
    """
    hostname_asserts_map: Dict[str, List[Assert]] = {}

    for hostname_assert in zip(
        cycle(hostnames),
        get_remaining_asserts(asserts, state[test_name].get("asserts", {})),
    ):
        hostname = hostname_assert[0]
        asrt = hostname_assert[1]

        if hostname not in hostname_asserts_map:
            hostname_asserts_map[hostname] = [asrt]
        else:
            hostname_asserts_map[hostname] += [asrt]

    asserts_task = (
        bag.from_sequence(hostname_asserts_map)
        .map(
            lambda h_name: run_asserts(
                hostname_asserts_map[h_name], {**state}, h_name, seconds_between_asserts
            )
        )
        .fold(combine_data_by_key, initial=state[test_name].get("asserts", {}))
    )

    return asserts_task.compute()


def verify_action_names(actions: List[Action], test_config: TestConfig):
    action_names = []

    for action in actions:
        assert (
            "type" in action
        ), f"Action in test '{test_config['name']}' is missing property 'type'"

        action_name = action.get("name")

        if action_name is None:
            action_name = create_item_name(action["type"], action_names)

        # NOTE: sets action name if not set
        action["name"] = action_name
        action_names.append(action_name)

        action_assert_names = []

        for i, asrt in enumerate(action.get("asserts", [])):
            assert_name = asrt.get("name")

            if assert_name is None:
                assert_name = f"Assert{i}"

            asrt["name"] = assert_name
            action_assert_names.append(assert_name)

        assert len(set(action_assert_names)) == len(
            action_assert_names
        ), f"Assert names for action {action_name} if specified must be unique"

    assert len(set(action_names)) == len(
        action_names
    ), "Action names if specified must be unique"


def verify_assert_names(asserts: List[Assert], test_config: TestConfig):
    assert_names = []

    for asrt in asserts:
        assert (
            "type" in asrt
        ), f"Assert in test '{test_config['name']}' is missing property 'type'"

        assert_name = asrt.get("name")

        if assert_name is None:
            assert_name = create_item_name(asrt["type"], assert_names)

        # NOTE: sets assert name if not set
        asrt["name"] = assert_name
        assert_names.append(assert_name)

    assert len(set(assert_names)) == len(
        assert_names
    ), "Assert names if specified must be unique"


def run_test(
    test_config: TestConfig,
    incoming_state: dict,
    hostnames: List[str],
    timeout_signal_name: str = None,
) -> dict:
    """
    Runs actions and asserts in provided test and returns new state with finished actions/asserts

    Args:
        test_config: test configuration to run
        incoming_state: Initial state of test (does not modify)
        hostnames: Addresses of runners to run actions/asserts on
        timeout_signal_name: Optional Dask variable to check if test has timed out so it can end gracefully

    Returns:
        New state after running actions and asserts
    """

    actions = test_config.get("actions", [])
    asserts = test_config.get("asserts", [])

    default_cycles = get_default_cycles(actions, asserts)

    remaining_cycles = test_config.get("cycles", default_cycles)
    completed_cycles = 0
    # NOTE: possibly use infinite default dict
    state = defaultdict(dict, incoming_state)

    # Validate test before running
    assert hostnames, "Must have at least one host to run tests"
    verify_action_names(actions, test_config)
    verify_assert_names(asserts, test_config)
    start_time = datetime.now()

    # stop if remaining_cycles == 0 or had asserts and no asserts remain
    while continue_running(
        actions,
        asserts,
        remaining_cycles,
        state[test_config["name"]].get("actions", {}),
        state[test_config["name"]].get("asserts", {}),
    ):
        # Check if running with a timeout and break if timeout has signaled
        if timeout_signal_name is not None:
            keep_going = Variable(timeout_signal_name, client=get_client())

            if not keep_going.get():
                break

        # NOTE: exceptions thrown in actions/asserts cause rest of test to exit
        action_distribution_strategy = test_config.get(
            "actionDistributionStrategy", "parallel"
        )

        if actions:
            assert action_distribution_strategy in [
                "parallel",
                "series",
            ], f"actionDistributionStrategy must be 'parallel' or 'series', got '{action_distribution_strategy}'"

            # TODO: option to run as many as possible in time period
            if action_distribution_strategy == "series":
                run_actions_func = run_actions_series
            else:
                run_actions_func = run_actions_parallel

            state[test_config["name"]]["actions"] = run_actions_func(
                actions,
                state,
                test_config["name"],
                hostnames,
                test_config.get("secondsBetweenActions", 0),
            )

        assert_distribution_strategy = test_config.get(
            "assertDistributionStrategy", "series"
        )

        if asserts:
            assert assert_distribution_strategy in [
                "parallel",
                "series",
            ], f"assertDistributionStrategy must be 'parallel' or 'series', got '{assert_distribution_strategy}'"

            if assert_distribution_strategy == "parallel":
                run_asserts_func = run_asserts_parallel
            else:
                run_asserts_func = run_asserts_series

            state[test_config["name"]]["asserts"] = run_asserts_func(
                asserts,
                state,
                test_config["name"],
                hostnames,
                test_config.get("secondsBetweenAsserts", 0),
            )

        remaining_cycles -= 1
        completed_cycles += 1

        # Wait between cycles if test is to continue running
        if continue_running(
            actions,
            asserts,
            remaining_cycles,
            state[test_config["name"]].get("actions", {}),
            state[test_config["name"]].get("asserts", {}),
        ):
            time.sleep(test_config.get("secondsBetweenCycles", 1))

    remaining_asserts = get_remaining_asserts(
        asserts, state[test_config["name"]].get("asserts", {})
    )

    state[test_config["name"]]["summary"] = TestSummary(
        description=test_config.get("description"),
        completed_cycles=completed_cycles,
        remaining_asserts=[asrt["name"] for asrt in remaining_asserts],
        error=None,
        duration=(datetime.now() - start_time).seconds,
        filename=test_config.get("filename"),
    )

    return state


def run_test_with_timeout(
    test_config: TestConfig,
    incoming_state: dict,
    hostnames: List[str],
    duration: int = 15,
) -> dict:
    """
    Calls run_test with a timeout and signals run_test to end gracefully if timeout has completed

    Args:
        test_config: Config of test to run
        incoming_state: Initial state to run actions/asserts in
        hostnames: List of runner hostnames
        duration: Optional timeout to run test within (I suppose this is to make it convenient to call in runners)

    Returns:
        New state after running actions and asserts
    """
    if duration is None or duration < 0:
        return run_test(test_config, incoming_state, hostnames)

    # NOTE: Use a dask cluster scheduler?
    client = get_client()

    # NOTE: may improve way of doing this
    timeout_signal_name = f"keep-going-{str(uuid.uuid4())}"
    keep_going = Variable(timeout_signal_name)
    keep_going.set(True)

    run_test_task: Future = client.submit(
        run_test,
        test_config=test_config,
        incoming_state=incoming_state,
        hostnames=hostnames,
        timeout_signal_name=timeout_signal_name,
    )

    LOGGER.debug("Test duration config: %d seconds", duration)

    def distributed_timeout():
        # If a timeout from a previous test did not complete, it will keep running (it cannot be canceled)
        # However, if it keeps running, it can end another test early
        # This means it needs to receive a signal to return
        end_time = datetime.now() + timedelta(seconds=duration)
        while datetime.now() <= end_time and keep_going.get():
            time.sleep(test_config.get("secondsBetweenCycles", 1))

    timeout_task: Future = client.submit(distributed_timeout)

    # Wait for either test or timeout to finish
    # Return test result if it finishes first
    # End test if timeout finishes first and return state
    start = datetime.now()
    wait([run_test_task, timeout_task], return_when="FIRST_COMPLETED")
    end = datetime.now()

    LOGGER.debug("Test %s took %d seconds", test_config["name"], (end - start).seconds)

    if run_test_task.done():
        keep_going.set(False)
        return run_test_task.result()
    elif timeout_task.done():
        LOGGER.debug("test task: %s", run_test_task)
        LOGGER.debug("timeout task: %s", timeout_task)
        LOGGER.info("Test %s timed out", test_config["name"])
        # NOTE: add timed out to summary?
        keep_going.set(False)
        return run_test_task.result()
