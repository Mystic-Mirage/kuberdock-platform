import json
from flask import current_app

from ..utils import modify_node_ips, run_ssh_command
from .pod import Pod
from .helpers import KubeQuery, ModelQuery, Utilities
from ..api.stream import send_event
from ..settings import KUBERDOCK_INTERNAL_USER, TRIAL_KUBES


class PodCollection(KubeQuery, ModelQuery, Utilities):

    def __init__(self, owner=None):
        self.owner = owner
        namespaces = self._get_namespaces()
        self._get_pods(namespaces)
        self._merge()

    def add(self, params):
        self._check_trial(params)
        namespace = '{0}-{1}-pods'.format(
            self.owner.username, params['name']).lower()
        params['namespace'] = namespace
        params['owner'] = self.owner
        pod = Pod.create(params)
        pod.compose_persistent(self.owner.username)
        self._save_pod(pod)
        pod._forge_dockers()
        if hasattr(pod, 'public_ip'):
            pod._allocate_ip()
        return pod.as_dict()

    def get(self, as_json=True):
        pods = [p.as_dict() for p in self._collection.values() if getattr(p, 'owner', '') == self.owner.username]
        if as_json:
            return json.dumps(pods)
        return pods

    def get_by_id(self, pod_id, as_json=False):
        try:
            pod = [p for p in self._collection.values() if p.id == pod_id][0]
            if as_json:
                return pod.as_json()
            return pod
        except IndexError:
            self._raise("No such item", 404)

    def update(self, pod_id, data):
        pod = self.get_by_id(pod_id)
        command = data.get('command')
        if command is None:
            return
        dispatcher = {
            'start': self._start_pod,
            'stop': self._stop_pod,
            'resize': self._resize_replicas,
            'container_start': self._container_start,
            'container_stop': self._container_stop,
            'container_delete': self._container_delete}
        if command in dispatcher:
            return dispatcher[command](pod, data)
        self._raise("Unknown command")

    def delete(self, pod_id, force=False):
        pod = self.get_by_id(pod_id)
        if pod.owner == KUBERDOCK_INTERNAL_USER and not force:
            self._raise('Service pod cannot be removed')
        if hasattr(pod, 'sid'):
            rv = self._del([pod.kind, pod.sid], use_v3=True, ns=pod.namespace)
            self._raise_if_failure(rv, "Could not remove a pod")
            if pod.cluster:
                self._stop_cluster(pod)
        service_name = pod.get_config('service')
        if service_name:
            service = self._get(['services', service_name], use_v3=True, ns=pod.namespace)
            state = json.loads(service.get('metadata', {}).get('annotations', {}).get('public-ip-state', '{}'))
            if 'assigned-to' in state:
                res = modify_node_ips(
                    state['assigned-to'], 'del',
                    state['assigned-pod-ip'],
                    state['assigned-public-ip'],
                    service.get('spec', {}).get('ports'))
                if not res:
                    self._raise("Can't unbind ip from node({0}). Connection error".format(state['assigned-to']))
            rv = self._del(['services', service_name], use_v3=True, ns=pod.namespace)
            self._raise_if_failure(rv, "Could not remove a service")
        if hasattr(pod, 'public_ip'):
            pod._free_ip()
        rv = self._drop_namespace(pod.namespace)
        # current_app.logger.debug(rv)
        self._mark_pod_as_deleted(pod_id)

    def _make_namespace(self, namespace):
        config = {
            "kind": "Namespace",
            "apiVersion": 'v1beta3',
            "metadata": {"name": namespace}}
        data = self._get_namespace(namespace)
        if data is None:
            rv = self._post(['namespaces'], json.dumps(config), rest=True, use_v3=True, ns=False)
            # current_app.logger.debug(rv)

    def _get_namespace(self, namespace):
        data = self._get(use_v3=True, ns=namespace)
        if data.get('code') == 404:
            return None
        return data

    def _get_namespaces(self):
        data = self._get(['namespaces'], use_v3=True, ns=False)
        if self.owner is None:
            return data['items']
        # check for uniqueness ?
        namespaces = []
        for namespace in data['items']:
            name = namespace.get('metadata', {}).get('name', '')
            if name.startswith(self.owner.username + '-') and name.endswith('-pods'):
                namespaces.append(name)
        return namespaces

    def _drop_namespace(self, namespace):
        rv = self._del(['namespaces', namespace], use_v3=True, ns=False)
        self._raise_if_failure(rv, "Cannot delete namespace '{}'".format(namespace))
        return rv

    def _get_replicas(self, name=None):
        # TODO: apply namespaces here
        replicas = []
        data = self._get(['replicationControllers'])

        for item in data['items']:
            try:
                replica_item = {
                    'id': item['uid'],
                    'sid': item['id'],
                    'replicas': item['currentState']['replicas'],
                    'replicaSelector': item['desiredState']['replicaSelector'],
                    'name': item['labels']['name']}

                if name is not None and replica_item['replicaSelector'] != name:
                    continue
                replicas.append(replica_item)
            except KeyError:
                pass
        return replicas

    def _get_pods(self, namespaces=None):
        # current_app.logger.debug(namespaces)
        if not hasattr(self, '_collection'):
            self._collection = {}
        pod_index = set()

        data = []
        services_data = []
        replicas_data = []

        if namespaces:
            for namespace in namespaces:
                data.extend(self._get(['pods'], use_v3=True, ns=namespace)['items'])
                services_data.extend(self._get(['services'], use_v3=True, ns=namespace)['items'])
                replicas_data.extend(self._get(['replicationcontrollers'], use_v3=True, ns=namespace)['items'])
        else:
            data.extend(self._get(['pods'], use_v3=True)['items'])
            services_data.extend(self._get(['services'], use_v3=True)['items'])
            replicas_data.extend(self._get(['replicationcontrollers'], use_v3=True)['items'])

        for item in data:
            pod = Pod.populate(item)

            for s in services_data:
                if self._is_related(item['metadata']['labels'], s['spec']['selector']):
                    pod.serviceIP = s['spec'].get('portalIP')
                    break

            for r in replicas_data:
                if self._is_related(item['metadata']['labels'], r['spec']['selector']):
                    pod.sid = r['metadata']['name']
                    pod.cluster = True
                    break

            if pod.sid not in pod_index:
                self._collection[pod.name, pod.namespace] = pod
                pod_index.add(pod.sid)

    def _merge(self):
        db_pods = self._fetch_pods(users=True)
        for db_pod in db_pods:
            namespace = db_pod.namespace
            if (db_pod.name, namespace) not in self._collection:
                pod = Pod(json.loads(db_pod.config))
                pod._forge_dockers()
                self._collection[pod.name, namespace] = pod
            else:
                self._collection[db_pod.name, namespace].id = db_pod.id
                # TODO if remove _is_related then add serviceIP attribute here
                # self._collection[db_pod.name, namespace].service = json.loads(db_pod.config).get('service')
                self._collection[db_pod.name, namespace].kube_type = json.loads(db_pod.config).get('kube_type')
                a = self._collection[db_pod.name, namespace].containers
                b = json.loads(db_pod.config).get('containers')
                self._collection[db_pod.name, namespace].containers = self.merge_lists(a, b, 'name')
            if not hasattr(self._collection[db_pod.name, namespace], 'owner'):
                self._collection[db_pod.name, namespace].owner = db_pod.owner.username
            if not hasattr(self._collection[db_pod.name, namespace], 'status'):
                self._collection[db_pod.name, namespace].status = 'stopped'

    def _run_service(self, pod):
        ports = []
        for ci, c in enumerate(getattr(pod, 'containers', [])):
            for pi, p in enumerate(c.get('ports', [])):
                host_port = p.get('hostPort', None) or p.get('containerPort')
                port_name = 'c{0}-p{1}'.format(ci, pi)
                if p.get('isPublic'):
                    port_name += '-public'
                ports.append({
                    "name": port_name,
                    "port": host_port,
                    "protocol": p.get('protocol'),
                    "targetPort": p.get('containerPort')})

        conf = {
            'kind': 'Service',
            'metadata': {
                # 'generateName': pod.name.lower() + '-service-',
                'generateName': 'service-',
                'labels': {'name': pod._make_dash(limit=54) + '-service'},
                'annotations': {
                    'public-ip-state': json.dumps({
                        'assigned-public-ip': getattr(pod, 'public_ip', None)
                    })
                },
            },
            'spec': {
                'selector': {'name': pod.name},
                'ports': ports,
                'sessionAffinity': 'None'   # may be ClientIP is better
            }
        }
        if hasattr(pod, 'portalIP') and pod.portalIP:
            conf['spec']['portalIP'] = pod.portalIP
        return self._post(['services'], json.dumps(conf), rest=True, use_v3=True, ns=pod.namespace)

    def _resize_replicas(self, pod, data):
        # FIXME: not working for now
        number = int(data.get('replicas', getattr(pod, 'replicas', 0)))
        replicas = self._get_replicas(pod.name)
        # TODO check replica numbers and compare to ones set in config
        for replica in replicas:
            rv = self._put(
                ['replicationControllers', replica.get('id', '')],
                json.loads({'desiredState': {'replicas': number}}))
            self._raise_if_failure(rv, "Could not resize a replica")
        return len(replicas)

    def _start_pod(self, pod, data=None):
        self._make_namespace(pod.namespace)
        if not pod.get_config('service'):
            for c in pod.containers:
                if len(c.get('ports', [])) > 0:
                    service_rv = self._run_service(pod)
                    self._raise_if_failure(service_rv, "Could not start a service")
                    self._update_pod_config(pod, **{'service': service_rv['metadata']['name']})
                    break
        config = pod.prepare()
        rv = self._post([pod.kind], json.dumps(config), rest=True, use_v3=True, ns=pod.namespace)
        # current_app.logger.debug(rv)
        self._raise_if_failure(rv, "Could not start '{0}' pod".format(pod.name))
        #return rv
        return {'status': 'pending'}

    def _stop_pod(self, pod, data=None):
        pod.status = 'stopped'
        if hasattr(pod, 'sid'):
            rv = self._del([pod.kind, pod.sid], use_v3=True, ns=pod.namespace)
            if pod.cluster:
                self._stop_cluster(pod)
            self._raise_if_failure(rv, "Could not stop a pod")
            #return rv
            return {'status': 'stopped'}

    def _stop_cluster(self, pod):
        for p in self._get(['pods'], use_v3=True, ns=pod.namespace)['items']:
            if self._is_related(p['metadata']['labels'], {'name': pod.name}):
                self._del(['pods', p['metadata']['name']], use_v3=True, ns=pod.namespace)

    def _do_container_action(self, action, data):
        host = data.get('host')
        if not host:
            return
        rv = {}
        containers = data.get('containers', '').split(',')
        for container in containers:
            command = 'docker {0} {1}'.format(action, container)
            status, message = run_ssh_command(host, command)
            if status != 0:
                self._raise('Docker error: {0} ({1}).'.format(message, status))
            if action in ('start', 'stop'):
                send_event('pull_pod_state', message)
            rv[container] = message or 'OK'
        return rv

    def _container_start(self, pod, data):
        self._do_container_action('start', data)

    def _container_stop(self, pod, data):
        self._do_container_action('stop', data)

    def _container_delete(self, pod, data):
        self._do_container_action('rm', data)

    @staticmethod
    def _is_related(one, two):
        if one is None or two is None:
            return False
        for k in two.keys():
            if k not in one:
                return False
            if one[k] != two[k]:
                return False
            return True

    def _check_trial(self, params):
        if self.owner.is_trial():
            user_kubes = self.owner.kubes
            kubes_left = TRIAL_KUBES - user_kubes
            pod_kubes = sum([c['kubes'] for c in params['containers']])
            if pod_kubes > kubes_left:
                self._raise('Trial User limit is exceeded. '
                            'Kubes available for you: {0}'.format(kubes_left))
