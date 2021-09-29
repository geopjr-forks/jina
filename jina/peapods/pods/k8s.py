import copy
import time
from argparse import Namespace
from typing import Optional, Dict, Union, Set, List

import jina
from .k8slib import kubernetes_deployment, kubernetes_tools
from ..pods import BasePod, ExitFIFO
from ... import __default_executor__
from ...logging.logger import JinaLogger


class K8sPod(BasePod, ExitFIFO):
    """The K8sPod (KubernetesPod)  is used for deployments on Kubernetes."""

    class _K8sDeployment:
        def __init__(
            self,
            name: str,
            head_port_in: int,
            tail_port_out: int,
            head_zmq_identity: bytes,
            version: str,
            shard_id: Optional[int],
            common_args: Union['Namespace', Dict],
            deployment_args: Union['Namespace', Dict],
        ):
            self.name = name
            self.dns_name = kubernetes_deployment.to_dns_name(name)
            self.head_port_in = head_port_in
            self.tail_port_out = tail_port_out
            self.head_zmq_identity = head_zmq_identity
            self.version = version
            self.shard_id = shard_id
            self.common_args = common_args
            self.deployment_args = deployment_args
            self.k8s_namespace = self.common_args.k8s_namespace
            self.num_replicas = getattr(self.common_args, 'replicas', 1)

        def _deploy_gateway(self):
            kubernetes_deployment.deploy_service(
                self.name,
                namespace=self.k8s_namespace,
                image_name=f'jinaai/jina:{self.version}-py38-standard',
                container_cmd='["jina"]',
                container_args=f'["gateway", '
                f'"--grpc-data-requests", '
                f'{kubernetes_deployment.get_cli_params(self.common_args, ("pod_role",))}]',
                logger=JinaLogger(f'deploy_{self.name}'),
                replicas=1,
                pull_policy='Always',
                port_expose=self.common_args.port_expose,
            )

        @staticmethod
        def _construct_runtime_container_args(
            deployment_args, uses, uses_metas, uses_with_string
        ):
            container_args = (
                f'["executor", '
                f'"--native", '
                f'"--uses", "{uses}", '
                f'"--grpc-data-requests", '
                f'"--runtime-cls", "GRPCDataRuntime", '
                f'"--uses-metas", "{uses_metas}", '
                + uses_with_string
                + f'{kubernetes_deployment.get_cli_params(deployment_args)}]'
            )
            return container_args

        def _deploy_runtime(self):
            image_name = kubernetes_deployment.get_image_name(self.deployment_args.uses)
            init_container_args = kubernetes_deployment.get_init_container_args(
                self.common_args
            )
            uses_metas = kubernetes_deployment.dictionary_to_cli_param(
                {'pea_id': self.shard_id}
            )
            uses_with = kubernetes_deployment.dictionary_to_cli_param(
                self.deployment_args.uses_with
            )
            uses_with_string = f'"--uses-with", "{uses_with}", ' if uses_with else ''
            if image_name == __default_executor__:
                image_name = f'jinaai/jina:{self.version}-py38-standard'
                uses = 'BaseExecutor'
            else:
                uses = 'config.yml'
            container_args = self._construct_runtime_container_args(
                self.deployment_args, uses, uses_metas, uses_with_string
            )

            kubernetes_deployment.deploy_service(
                self.dns_name,
                namespace=self.k8s_namespace,
                image_name=image_name,
                container_cmd='["jina"]',
                container_args=container_args,
                logger=JinaLogger(f'deploy_{self.name}'),
                replicas=self.num_replicas,
                pull_policy='IfNotPresent',
                init_container=init_container_args,
                custom_resource_dir=getattr(
                    self.common_args, 'k8s_custom_resource_dir', None
                ),
            )

        def wait_start_success(self):
            client = kubernetes_tools.K8sClients().core_v1
            pod_ips = set()
            while True:
                for pod_info in client.list_namespaced_pod(
                    self.k8s_namespace,
                ).items:
                    # filter if this pod_info corresponds to me to take care.
                    if (
                        pod_info.metadata.labels['app'] == self.name
                        and pod_info.status.phase == 'Running'
                    ):
                        pod_ips.add(pod_info.status.pod_ip)
                        if len(pod_ips) == self.num_replicas:
                            return
                time.sleep(0.1)

        def start(self):
            if self.name == 'gateway':
                self._deploy_gateway()
            else:
                self._deploy_runtime()
            if not self.common_args.noblock_on_start:
                self.wait_start_success()
            return self

        def close(self):
            with JinaLogger(f'close_{self.name}') as logger:
                try:
                    client = kubernetes_tools.K8sClients().apps_v1
                    resp = client.delete_namespaced_deployment(
                        name=self.name, namespace=self.k8s_namespace
                    )
                    if resp.status == 'Success':
                        logger.success(
                            f' Successful deletion of deployment {self.name}'
                        )
                    else:
                        logger.error(
                            f' Deletion of deployment {self.name} unsuccessful with status {resp.status}'
                        )
                except Exception as exc:
                    logger.error(f' Error deleting deployment {self.name}: {repr(exc)}')

        def __enter__(self):
            return self.start()

        def __exit__(self, exc_type, exc_val, exc_tb):
            self.close()

        def to_node(self):
            return {
                'name': self.name,
                'head_host': f'{self.dns_name}.{self.k8s_namespace}.svc',
                'head_port_in': self.head_port_in,
                'tail_port_out': self.tail_port_out,
                'head_zmq_identity': self.head_zmq_identity,
            }

    def __init__(
        self, args: Union['Namespace', Dict], needs: Optional[Set[str]] = None
    ):
        super().__init__()
        self.args = args
        self.needs = needs or set()
        self.deployment_args = self._parse_args(args)
        self.version = self._get_base_executor_version()

        self.fixed_head_port_in = 8081
        self.fixed_tail_port_out = 8082
        self.k8s_head_deployment = None
        self.k8s_tail_deployment = None
        if self.deployment_args['head_deployment'] is not None:
            name = f'{self.name}-head'
            self.k8s_head_deployment = self._K8sDeployment(
                name=name,
                head_port_in=self.fixed_head_port_in,
                tail_port_out=self.fixed_tail_port_out,
                head_zmq_identity=self.head_zmq_identity,
                version=self.version,
                shard_id=None,
                common_args=self.args,
                deployment_args=self.deployment_args['head_deployment'],
            )
        if self.deployment_args['tail_deployment'] is not None:
            name = f'{self.name}-tail'
            self.k8s_tail_deployment = self._K8sDeployment(
                name=name,
                head_port_in=self.fixed_head_port_in,
                tail_port_out=self.fixed_tail_port_out,
                head_zmq_identity=self.head_zmq_identity,
                version=self.version,
                shard_id=None,
                common_args=self.args,
                deployment_args=self.deployment_args['tail_deployment'],
            )

        self.k8s_deployments = []
        for i, args in enumerate(self.deployment_args['deployments']):
            name = (
                f'{self.name}-{i}'
                if len(self.deployment_args['deployments']) > 1
                else f'{self.name}'
            )
            self.k8s_deployments.append(
                self._K8sDeployment(
                    name=name,
                    head_port_in=self.fixed_head_port_in,
                    tail_port_out=self.fixed_tail_port_out,
                    head_zmq_identity=self.head_zmq_identity,
                    version=self.version,
                    shard_id=None,
                    common_args=self.args,
                    deployment_args=args,
                )
            )

    def _parse_args(
        self, args: Namespace
    ) -> Dict[str, Optional[Union[List[Namespace], Namespace]]]:
        return self._parse_deployment_args(args)

    def _parse_deployment_args(self, args):
        parsed_args = {
            'head_deployment': None,
            'tail_deployment': None,
            'deployments': [],
        }
        parallel = getattr(args, 'parallel', 1)
        replicas = getattr(args, 'replicas', 1)
        uses_before = getattr(args, 'uses_before', None)
        if parallel > 1 or (len(self.needs) > 1 and replicas > 1) or uses_before:
            # reasons to separate head and tail from peas is that they
            # can be deducted based on the previous and next pods
            parsed_args['head_deployment'] = copy.copy(args)
            parsed_args['head_deployment'].uses = (
                args.uses_before or __default_executor__
            )
        if parallel > 1 or getattr(args, 'uses_after', None):
            parsed_args['tail_deployment'] = copy.copy(args)
            parsed_args['tail_deployment'].uses = (
                args.uses_after or __default_executor__
            )

        parsed_args['deployments'] = [args] * parallel
        return parsed_args

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        super().__exit__(exc_type, exc_val, exc_tb)
        self.join()

    @property
    def port_expose(self) -> int:
        """Not implemented"""
        raise NotImplementedError

    @property
    def host(self) -> str:
        """Not implemented"""
        raise NotImplementedError

    def start(self) -> 'K8sPod':
        """Deploy the kubernetes pods via k8s Deployment and k8s Service.

        :return: self
        """
        with JinaLogger(f'start_{self.name}') as logger:
            logger.info(
                f'🏝️\tCreate Namespace "{self.args.k8s_namespace}" for "{self.name}"'
            )
            kubernetes_tools.create(
                'namespace',
                {'name': self.args.k8s_namespace},
                logger=logger,
                custom_resource_dir=getattr(self.args, 'k8s_custom_resource_dir', None),
            )
            if self.k8s_head_deployment is not None:
                self.enter_context(self.k8s_head_deployment)
            for k8s_deployment in self.k8s_deployments:
                self.enter_context(k8s_deployment)
            if self.k8s_tail_deployment is not None:
                self.enter_context(self.k8s_tail_deployment)
        return self

    def wait_start_success(self):
        """Not implemented. It should wait until the deployment is up and running"""
        if not self.args.noblock_on_start:
            raise ValueError(
                f'{self.wait_start_success!r} should only be called when `noblock_on_start` is set to True'
            )
        try:
            if self.k8s_head_deployment is not None:
                self.k8s_head_deployment.wait_start_success()
            for p in self.k8s_deployments:
                p.wait_start_success()
            if self.k8s_tail_deployment is not None:
                self.k8s_tail_deployment.wait_start_success()
        except:
            self.close()
            raise

    def join(self):
        """Not needed. The context managers will manage the proper deletion"""
        pass

    def update_pea_args(self):
        """
        Regenerate deployment args
        """
        self.deployment_args = self._parse_args(self.args)

    @property
    def head_args(self) -> Namespace:
        """Head args of the pod.

        :return: namespace
        """
        return self.args

    @property
    def tail_args(self) -> Namespace:
        """Tail args of the pod

        :return: namespace
        """
        return self.args

    @property
    def num_peas(self) -> int:
        """Number of peas. Currently unused.

        :return: number of peas
        """
        return -1

    @property
    def head_zmq_identity(self) -> bytes:
        """zmq identity is not needed for k8s deployment

        :return: zmq identity
        """
        return b''

    @property
    def deployments(self) -> List[Dict]:
        """Deployment information which describes the interface of the pod.

        :return: list of dictionaries defining the attributes used by the routing table
        """
        res = []

        if self.args.name == 'gateway':
            res.append(self.k8s_deployments[0].to_node())
        else:
            if self.k8s_head_deployment:
                res.append(self.k8s_head_deployment.to_node())
            res.extend([_.to_node() for _ in self.k8s_deployments])
            if self.k8s_tail_deployment:
                res.append(self.k8s_tail_deployment.to_node())
        return res

    def _get_base_executor_version(self):
        import requests

        url = 'https://registry.hub.docker.com/v1/repositories/jinaai/jina/tags'
        tags = requests.get(url).json()
        name_set = {tag['name'] for tag in tags}
        if jina.__version__ in name_set:
            return jina.__version__
        else:
            return 'master'

    @property
    def _mermaid_str(self) -> List[str]:
        """String that will be used to represent the Pod graphically when `Flow.plot()` is invoked


        .. # noqa: DAR201
        """
        mermaid_graph = []
        if self.name != 'gateway':
            mermaid_graph = [f'subgraph {self.name};\n', f'direction LR;\n']

            num_replicas = getattr(self.args, 'replicas', 1)
            num_shards = getattr(self.args, 'parallel', 1)
            uses = self.args.uses
            if num_shards > 1:
                shard_names = [
                    f'{args.name}/shard-{i}'
                    for i, args in enumerate(self.deployment_args['deployments'])
                ]
                for shard_name in shard_names:
                    shard_mermaid_graph = [
                        f'subgraph {shard_name}\n',
                        f'direction TB;\n',
                    ]
                    for replica_id in range(num_replicas):
                        shard_mermaid_graph.append(
                            f'{shard_name}/replica-{replica_id}[{uses}]\n'
                        )
                    shard_mermaid_graph.append(f'end\n')
                    mermaid_graph.extend(shard_mermaid_graph)
                head_name = f'{self.name}/head'
                tail_name = f'{self.name}/tail'
                head_to_show = self.args.uses_before
                if head_to_show is None or head_to_show == __default_executor__:
                    head_to_show = head_name
                tail_to_show = self.args.uses_after
                if tail_to_show is None or tail_to_show == __default_executor__:
                    tail_to_show = tail_name
                if head_name:
                    for shard_name in shard_names:
                        mermaid_graph.append(
                            f'{head_name}[{head_to_show}]:::HEADTAIL --> {shard_name}[{uses}];'
                        )

                if tail_name:
                    for shard_name in shard_names:
                        mermaid_graph.append(
                            f'{shard_name}[{uses}] --> {tail_name}[{tail_to_show}]:::HEADTAIL;'
                        )
            else:
                for replica_id in range(num_replicas):
                    mermaid_graph.append(f'{self.name}/replica-{replica_id}[{uses}];')

            mermaid_graph.append(f'end;')
        return mermaid_graph
