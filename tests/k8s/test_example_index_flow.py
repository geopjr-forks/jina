import time

import pytest
from http import HTTPStatus

import requests

from jina import Flow


@pytest.fixture()
def k8s_flow_with_needs(test_executor_image: str, executor_merger_image: str) -> Flow:
    flow = (
        Flow(name='test-flow', port_expose=8080, infrastructure='K8S', protocol='http')
        .add(
            name='segmenter',
            uses=test_executor_image,
        ).add(
            name='textencoder',
            uses=test_executor_image,
            needs='segmenter',
        )
        .add(
            name='textstorage',
            uses=test_executor_image,
            needs='textencoder',
        )
        .add(
            name='imageencoder',
            uses=test_executor_image,
            needs='segmenter',
        )
        .add(
            name='imagestorage',
            uses=test_executor_image,
            needs='imageencoder',
        ).
        add(
            name='merger',
            uses=executor_merger_image,
            needs=['imagestorage', 'textstorage']
        )
    )
    return flow


@pytest.mark.timeout(360)
def test_flow_with_needs(k8s_cluster_namespaced, test_executor_image, executor_merger_image, k8s_flow_with_needs: Flow, logger):
    expected_running_pods = 7

    # image pull anyways must be Never or IfNotPresent otherwise kubernetes will try to pull the image anyway
    logger.debug(f'Loading docker image into kind cluster...')
    k8s_cluster_namespaced.needs_docker_image(test_executor_image)
    k8s_cluster_namespaced.needs_docker_image(executor_merger_image)
    logger.debug(f'Done loading docker image into kind cluster...')

    logger.debug(f'Starting flow on kind cluster...')
    k8s_flow_with_needs.start()
    logger.debug(f'Done starting flow on kind cluster...')

    logger.debug(f'Starting to wait for pods in kind cluster to reach "RUNNING" state...')
    waiting = True
    while waiting:
        num_running_pods = len(k8s_cluster_namespaced.list_ready_pods(namespace=k8s_flow_with_needs.args.name))
        if num_running_pods == expected_running_pods:
            waiting = False
        time.sleep(3)
        logger.debug(f'Still waiting for pods to reach running state '
                     f'(Current Status: {num_running_pods}/{expected_running_pods}).')

    expected_traversed_executors = {'segmenter', 'imageencoder', 'textencoder', 'imagestorage', 'textstorage'}

    logger.debug(f'Starting port-forwarding to gateway service...')
    with k8s_cluster_namespaced.port_forward('service/gateway', 8080, 8080, k8s_flow_with_needs.args.name) as _:
        logger.debug(f'Port-forward running...')

        resp = requests.post(f'http://localhost:8080/simpleTag', json={'data': [{} for _ in range(10)]})

    assert resp.status_code == HTTPStatus.OK
    docs = resp.json()['data']['docs']
    assert len(docs) == 10
    for doc in docs:
        assert set(doc['tags']['traversed-executors']) == expected_traversed_executors


@pytest.fixture()
def k8s_flow_with_init_container(test_executor_image: str, executor_merger_image: str, dummy_dumper_image: str) -> Flow:
    flow = (
        Flow(name='test-flow', port_expose=8080, infrastructure='K8S', protocol='http')
        .add(
            name='test_executor',
            uses=test_executor_image,
            k8s_init_container_command=["python", "dump.py", "/shared/test_file.txt"],
            k8s_uses_init=dummy_dumper_image,
            k8s_mount_path='/shared'
        )
    )
    return flow


@pytest.mark.timeout(3600)
def test_flow_with_init(k8s_cluster_namespaced, test_executor_image, dummy_dumper_image: str, k8s_flow_with_init_container: Flow, logger):
    expected_running_pods = 2

    # image pull anyways must be Never or IfNotPresent otherwise kubernetes will try to pull the image anyway
    logger.debug(f'Loading docker image {test_executor_image} into kind cluster...')
    k8s_cluster_namespaced.needs_docker_image(test_executor_image)
    k8s_cluster_namespaced.needs_docker_image(dummy_dumper_image)
    logger.debug(f'Done loading docker image {test_executor_image} into kind cluster...')

    logger.debug(f'Starting flow on kind cluster...')
    k8s_flow_with_init_container.start()
    logger.debug(f'Done starting flow on kind cluster...')

    logger.debug(f'Starting to wait for pods in kind cluster to reach "RUNNING" state...')
    waiting = True
    while waiting:
        num_running_pods = len(k8s_cluster_namespaced.list_ready_pods(namespace=k8s_flow_with_init_container.args.name))
        if num_running_pods == expected_running_pods:
            waiting = False
        time.sleep(3)
        logger.debug(f'Still waiting for pods to reach running state '
                     f'(Current Status: {num_running_pods}/{expected_running_pods}).')

    logger.debug(f'Starting port-forwarding to gateway service...')
    with k8s_cluster_namespaced.port_forward('service/gateway', 8080, 8080, k8s_flow_with_init_container.args.name) as _:
        logger.debug(f'Port-forward running...')

        resp = requests.post('http://localhost:8080/readFile', json={'data': [{} for _ in range(10)]})

    assert resp.status_code == HTTPStatus.OK
    docs = resp.json()['data']['docs']
    assert len(docs) == 10
    for doc in docs:
        assert doc['tags']['file'] == '1\n2\n3'


@pytest.fixture()
def k8s_flow_with_sharding(test_executor_image: str, executor_merger_image: str, dummy_dumper_image: str) -> Flow:
    flow = (
        Flow(name='test-flow', port_expose=8080, infrastructure='K8S', protocol='http')
        .add(
            name='image_data',
            shards=3,
            replicas=2,
            polling='all',
            uses=test_executor_image,
            uses_after=executor_merger_image
        )
    )
    return flow


@pytest.mark.timeout(360)
def test_flow_with_sharding(k8s_cluster_namespaced, test_executor_image, executor_merger_image, k8s_flow_with_sharding: Flow, logger):
    expected_running_pods = 5

    # image pull anyways must be Never or IfNotPresent otherwise kubernetes will try to pull the image anyway
    logger.debug(f'Loading docker image into kind cluster...')
    k8s_cluster_namespaced.needs_docker_image(test_executor_image)
    k8s_cluster_namespaced.needs_docker_image(executor_merger_image)
    logger.debug(f'Done loading docker image into kind cluster...')

    logger.debug(f'Starting flow on kind cluster...')
    k8s_flow_with_sharding.start()
    logger.debug(f'Done starting flow on kind cluster...')

    logger.debug(f'Starting to wait for pods in kind cluster to reach "RUNNING" state...')
    waiting = True
    while waiting:
        num_running_pods = len(k8s_cluster_namespaced.list_ready_pods(namespace=k8s_flow_with_needs.args.name))
        if num_running_pods == expected_running_pods:
            waiting = False
        time.sleep(3)
        logger.debug(f'Still waiting for pods to reach running state '
                     f'(Current Status: {num_running_pods}/{expected_running_pods}).')

    expected_traversed_executors = {'segmenter', 'imageencoder', 'textencoder', 'imagestorage', 'textstorage'}

    logger.debug(f'Starting port-forwarding to gateway service...')
    with k8s_cluster_namespaced.port_forward('service/gateway', 8080, 8080, k8s_flow_with_needs.args.name) as _:
        logger.debug(f'Port-forward running...')

        resp = requests.post(f'http://localhost:8080/simpleTag', json={'data': [{} for _ in range(10)]})

    assert resp.status_code == HTTPStatus.OK
    docs = resp.json()['data']['docs']
    assert len(docs) == 10
    for doc in docs:
        assert set(doc['tags']['traversed-executors']) == expected_traversed_executors
