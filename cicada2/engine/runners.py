import uuid
import time
from typing import Dict, List, Optional

import docker
from docker.errors import APIError
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException

from cicada2.engine.config import (
    CONTAINER_NETWORK,
    CREATE_NETWORK,
    HEALTHCHECK_INITIAL_WAIT,
    HEALTHCHECK_MAX_RETRIES,
    POD_NAMESPACE,
    POD_SERVICE_ACCOUNT,
)
from cicada2.shared.errors import ValidationError
from cicada2.shared.logs import get_logger
from cicada2.engine.messaging import runner_healthcheck
from cicada2.engine.parsing import render_section
from cicada2.engine.testing import run_test_with_timeout
from cicada2.shared.types import TestConfig, RunnerClosure, TestSummary, Volume


LOGGER = get_logger("runners")


def runner_to_image(runner_name: str) -> Optional[str]:
    """
    Determine docker image based on runner name

    Args:
        runner_name: Type of test runner

    Returns:
        Docker image for runner
    """
    if runner_name == "rest-runner":
        return "cicadatesting/cicada-2-rest-runner"
    elif runner_name == "sql-runner":
        return "cicadatesting/cicada-2-sql-runner"
    elif runner_name == "kafka-runner":
        return "cicadatesting/cicada-2-kafka-runner"
    elif runner_name == "s3-runner":
        return "cicadatesting/cicada-2-s3-runner"
    elif runner_name == "grpc-runner":
        return "cicadatesting/cicada-2-grpc-runner"

    return None


def config_to_runner_env(config: Dict[str, str]) -> Dict[str, str]:
    """
    Converts runner config to standard env vars (prefixed with 'RUNNER_')

    Args:
        config: Runner config dictionary

    Returns:
        Formatted env map for runner
    """
    return {f"RUNNER_{key.upper()}": config[key] for key in config}


def container_is_healthy(
    hostname: str,
    initial_wait_time: int = HEALTHCHECK_INITIAL_WAIT,
    max_retries: int = HEALTHCHECK_MAX_RETRIES,
) -> bool:
    """
    Determines if a container is ready to accept messages using an exponential backoff

    Args:
        hostname: Address of runner
        initial_wait_time: Amount of seconds to wait before checking runner
        max_retries: Number of times to check runner

    Returns:
        If the runner is ready
    """
    retries = 0
    wait_time = initial_wait_time

    while retries < max_retries:
        time.sleep(wait_time)
        ready = runner_healthcheck(hostname)

        if not ready:
            retries += 1
            # NOTE: make multiplier configurable too?
            wait_time *= 2
        else:
            return True

    return False


def create_docker_container(
    image: str,
    env_map: Dict[str, str],
    run_id: str,
    network: str = CONTAINER_NETWORK,
    create_network: bool = CREATE_NETWORK,
    volumes: List[Volume] = None,
):
    """
    Creates and configures docker container for docker runner

    Args:
        client: Docker client
        image: docker image to launch
        env_map: env vars to provide to container
        run_id: cicada run ID (to provide as a tag to the container)
        network: Docker network to add container to
        create_network: Creates the network if not found if set to True
        volumes: List of absolute paths to directories on local machine to share with runner container

    Returns:
        Docker container object
    """
    # NOTE: client may need more config options (probably get from env)
    client: docker.DockerClient = docker.from_env()

    try:
        try:
            client.networks.get(network)
        except docker.errors.NotFound:
            if create_network:
                client.networks.create(network)
                LOGGER.info("Created docker network %s", network)
            else:
                raise ValidationError(f"Docker network {network} not configured")
    except APIError as err:
        raise RuntimeError(f"Unable to configure docker network: {err}")

    # Parse the part after the last repository path ('/') before the tag (':')
    runner_type = f"{image.split('/')[-1].split(':')[0]}"
    container_id = f"{runner_type}-{str(uuid.uuid4())[:8]}"

    if not volumes:
        volume_map = {}
    else:
        volume_map = {
            vol["source"]: {"bind": vol["destination"], "mode": "rw"} for vol in volumes
        }

    try:
        # Start container (will pull image if necessary)
        LOGGER.debug("Starting Docker container with image %s", image)

        container = client.containers.run(
            image,
            name=container_id,
            detach=True,
            environment=env_map,
            network=network,
            labels=["cicada-2-runner", run_id],
            volumes=volume_map,
        )
    except APIError as err:
        raise RuntimeError(f"Unable to create container: {err}")

    LOGGER.debug("healthchecking container %s", container.name)

    if container_is_healthy(f"{container_id}:50051"):
        LOGGER.info("successfully created container %s", container.name)
        return container
    else:
        raise RuntimeError("Unable to successfully contact container")


def stop_docker_container(container):
    LOGGER.debug("Stopping container %s", container.name)
    container.stop(timeout=3)


def clean_docker_containers(run_id):
    client: docker.DockerClient = docker.from_env()

    LOGGER.debug("Cleaning containers for run ID %s", run_id)

    try:
        containers = client.containers.list(filters={"label": run_id})

        for container in containers:
            container.stop()
    except APIError as err:
        raise RuntimeError(f"Unable to stop containers for run ID {run_id}: {err}")


def get_docker_hostname(container):
    return f"{container.name}:50051"


def create_kube_pod(
    image: str,
    env_map: Dict[str, str],
    run_id: str,
    namespace: str = POD_NAMESPACE,
    volumes: List[Volume] = None,
    service_account: str = POD_SERVICE_ACCOUNT,
):
    k8s_config.load_incluster_config()
    v1 = k8s_client.CoreV1Api()

    runner_type = f"{image.split('/')[-1].split(':')[0]}"
    container_id = f"{runner_type}-{str(uuid.uuid4())[:8]}"

    if not volumes:
        volumes = []
    else:
        volumes = [
            k8s_client.V1Volume(
                name=vol["source"],
                persistent_volume_claim=k8s_client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=vol["source"]
                ),
            )
            for vol in volumes
        ]

    volume_mounts = [
        k8s_client.V1VolumeMount(name=vol["source"], mount_path=vol["destination"])
        for vol in volumes
    ]

    pod_env = [
        k8s_client.V1EnvVar(name=key, value=value) for key, value in env_map.items()
    ]

    pod_body = k8s_client.V1Pod(
        metadata=k8s_client.V1ObjectMeta(
            name=container_id,
            labels={
                "run_id": run_id,
                "run": container_id,
                "family": "cicada",
                "type": "cicada-2-runner",
            },
        ),
        spec=k8s_client.V1PodSpec(
            containers=[
                k8s_client.V1Container(
                    image=image,
                    name=container_id,
                    ports=[k8s_client.V1ContainerPort(container_port=50051)],
                    volume_mounts=volume_mounts,
                    env=pod_env,
                )
            ],
            volumes=volumes,
            service_account_name=service_account,
        ),
    )

    service_body = k8s_client.V1Service(
        metadata=k8s_client.V1ObjectMeta(
            name=container_id,
            labels={"run_id": run_id, "family": "cicada", "type": "cicada-2-runner"},
        ),
        spec=k8s_client.V1ServiceSpec(
            ports=[k8s_client.V1ServicePort(port=50051, target_port=50051)],
            selector={"run": container_id},
        ),
    )

    try:
        # TODO: possibly implement built-in healthchecks
        pod = v1.create_namespaced_pod(namespace, pod_body)

        while pod.status.phase != "Running":
            time.sleep(1)
            pod = v1.read_namespaced_pod(container_id, namespace)

        v1.create_namespaced_service(namespace, service_body)
    except ApiException as err:
        raise RuntimeError(f"Unable to create pod: {err}")

    if container_is_healthy(f"{container_id}:50051"):
        LOGGER.info("successfully created pod %s", container_id)
        return container_id
    else:
        raise RuntimeError("Unable to successfully contact container")


def stop_kube_pod(container_id: str, namespace: str = POD_NAMESPACE):
    k8s_config.load_incluster_config()
    v1 = k8s_client.CoreV1Api()

    LOGGER.debug("Stopping pod and service %s", container_id)

    try:
        v1.delete_namespaced_pod(namespace=namespace, name=container_id)
        v1.delete_namespaced_service(namespace=namespace, name=container_id)
    except ApiException as err:
        raise RuntimeError(f"Unable to stop pod {container_id}: {err}")


# NOTE: commented out because can be handeled by operator - Maybe keep?
# def clean_kube_runners(run_id: str, namespace: str = POD_NAMESPACE):
#     k8s_config.load_incluster_config()
#     v1 = k8s_client.CoreV1Api()

#     LOGGER.debug("Cleaning pods and services for run ID %s", run_id)

#     try:
#         v1.delete_collection_namespaced_pod(
#             namespace=namespace, label_selector=f"run_id={run_id}"
#         )

#         service_list = v1.list_namespaced_service(
#             namespace=namespace, label_selector=f"run_id={run_id}"
#         ).items

#         for service in service_list:
#             v1.delete_namespaced_service(
#                 name=service.metadata.name, namespace=namespace
#             )
#     except ApiException as err:
#         raise RuntimeError(
#             f"Unable to stop pods and services for run ID {run_id}: {err}"
#         )


def get_pod_hostname(container_id: str):
    return f"{container_id}:50051"


def run_test(
    create_runner_fn,
    remove_runner_fn,
    get_runner_hostname_fn,
    test_config: TestConfig,
    run_id: str,
) -> RunnerClosure:
    """
    Runs test using docker runners

    Args:
        test_config: config of test to run
        run_id: cicada run ID

    Returns:
        Function to run test using state gathered from previous tests
    """

    def closure(state):
        try:
            rendered_test_config: TestConfig = render_section(test_config, state)

            image = runner_to_image(
                rendered_test_config.get("runner")
            ) or rendered_test_config.get("image")

            assert image is not None, "Must specify a valid 'runner' or 'image'"

            env = config_to_runner_env(
                render_section(rendered_test_config.get("config", {}), state)
            )

            runners = []

            for _ in range(rendered_test_config.get("runnerCount", 1)):
                runner = create_runner_fn(
                    image, env, run_id, volumes=rendered_test_config.get("volumes")
                )
                runners.append(runner)

            try:
                new_state = run_test_with_timeout(
                    test_config=rendered_test_config,
                    incoming_state=state,
                    hostnames=[get_runner_hostname_fn(runner) for runner in runners],
                    duration=rendered_test_config.get("timeout", 15),
                )
            except (AssertionError, ValueError, TypeError, RuntimeError) as err:
                # NOTE: May need to fine tune exception types
                LOGGER.error(
                    "Error running test %s: %s", test_config["name"], err, exc_info=True
                )

                for runner in runners:
                    remove_runner_fn(runner)

                new_state = {
                    test_config["name"]: {
                        "summary": TestSummary(
                            description=rendered_test_config.get("description"),
                            error=str(err),
                            completed_cycles=0,
                            remaining_asserts=[],
                            duration=0,
                            filename=rendered_test_config.get("filename"),
                        )
                    }
                }

            for runner in runners:
                remove_runner_fn(runner)
        except (AssertionError, ValueError, TypeError, RuntimeError) as err:
            LOGGER.error(
                "Error creating test %s: %s", test_config["name"], err, exc_info=True
            )

            new_state = {
                test_config["name"]: {
                    "summary": TestSummary(
                        description=test_config.get("description"),
                        error=str(err),
                        completed_cycles=0,
                        remaining_asserts=[],
                        duration=0,
                        filename=test_config.get("filename"),
                    )
                }
            }

        return {**state, **new_state}

    return closure
