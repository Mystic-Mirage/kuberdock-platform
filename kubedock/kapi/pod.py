
# KuberDock - is a platform that allows users to run applications using Docker
# container images and create SaaS / PaaS based on these applications.
# Copyright (C) 2017 Cloud Linux INC
#
# This file is part of KuberDock.
#
# KuberDock is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# KuberDock is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with KuberDock; if not, see <http://www.gnu.org/licenses/>.

import json
import os
import shlex
import uuid
from copy import deepcopy

from flask import current_app

import pstorage
from . import helpers
from . import podutils
from .helpers import KubeQuery, K8sSecretsClient, K8sSecretsBuilder
from .. import billing
from .. import settings
from ..billing.models import Kube
from ..exceptions import APIError, ServicePodDumpError
from ..pods.models import (db, PersistentDisk, PersistentDiskStatuses,
                           Pod as DBPod)
from ..utils import POD_STATUSES, nested_dict_utils

ORIGIN_ROOT = 'originroot'
OVERLAY_PATH = u'/var/lib/docker/overlay/{}/root'
MOUNT_KDTOOLS_PATH = '/.kdtools'
HOST_KDTOOLS_PATH = '/usr/lib/kdtools'
SERVICE_ACCOUNT_STUB_PATH = '/var/run/secrets/kubernetes.io/serviceaccount'


class PodOwner(dict):
    """Inherited from dict so as it will be presented as dict in
    `as_dict` method.
    """

    def __init__(self, id, username):
        super(PodOwner, self).__init__(id=id, username=username)
        self.id = id
        self.username = username

    def is_internal(self):
        return self.username == settings.KUBERDOCK_INTERNAL_USER


class VolumeExists(APIError):
    message_template = u'Volume with name "{name}" already exists'
    status_code = 409

    def __init__(self, volume_name=None, volume_id=None):
        details = {'name': volume_name, 'id': volume_id}
        super(VolumeExists, self).__init__(details=details)


class Pod(object):
    """
    Represents related k8s resources: RC, Service and all replicas (Pods).

    TODO: document other attributes

    id - uuid4, id in db
    namespace - uuid4, for now it's the same as `id`
    name - kubedock.pods.models.Pod.name (name in UI)
    owner - PodOwnerTuple()
    podIP - k8s.Service.spec.clusterIP (appears after first start)
    service - k8s.Service.metadata.name (appears after first start)
    sid - uuid4, k8s.ReplicationController.metadata.name
    secrets - list of k8s.Secret.name
    kube_type - Kube Type id
    volumes_public - public volumes data
    volumes -
        before self.compose_persistent() -- see volumes_public
        after -- volumes spec prepared for k8s
    k8s_status - current status in k8s or None
    db_status - current status in db or None
    status - common status composed from k8s_status and db_status
    ...

    """

    def __init__(self, data=None):
        self.k8s_status = None
        self.certificate = None
        owner = None
        if data is not None:
            for c in data['containers']:
                if len(c.get('args', [])) == 1:
                    # it seems the args has been changed
                    # or may be its length is only 1 item
                    c['args'] = self._parse_cmd_string(c['args'][0])
            if 'owner' in data:
                owner = data.pop('owner')
            for k, v in data.items():
                setattr(self, k, v)
        self.set_owner(owner)

    def set_owner(self, owner):
        """Set owner field as a named tuple with minimal necessary fields.

        It is needed to pass Pod object to async celery task, to prevent
        DetachedInstanceError, because of another session in celery task.

        :param owner: object of 'User' model
        """
        if owner is not None:
            self.owner = PodOwner(id=owner.id, username=owner.username)
        else:
            self.owner = PodOwner(None, None)

    @staticmethod
    def populate(data):
        """Create Pod object using Pod from Kubernetes."""
        pod = Pod()
        metadata = data.get('metadata', {})
        status = data.get('status', {})
        spec = data.get('spec', {})

        pod.sid = metadata.get('name')
        pod.namespace = metadata.get('namespace')
        pod.labels = metadata.get('labels', {})
        pod.id = pod.labels.get('kuberdock-pod-uid')

        pod.status = status.get('phase', POD_STATUSES.pending).lower()
        pod.k8s_status = pod.status
        pod.hostIP = status.get('hostIP')

        # TODO why we call this "pod.host" instead of "pod.nodeName" ?
        # rename it
        pod.host = spec.get('nodeName')
        pod.kube_type = spec.get('nodeSelector', {}).get('kuberdock-kube-type')
        # TODO we should use nodeName or hostIP instead, and rename this attr
        pod.node = spec.get('nodeSelector', {}).get('kuberdock-node-hostname')
        pod.volumes = spec.get('volumes', [])
        pod.containers = spec.get('containers', [])
        pod.restartPolicy = spec.get('restartPolicy')
        pod.dnsPolicy = spec.get('dnsPolicy')
        pod.serviceAccount = spec.get('serviceAccount', False)
        pod.hostNetwork = spec.get('hostNetwork')

        if pod.status in (POD_STATUSES.running, POD_STATUSES.succeeded,
                          POD_STATUSES.failed):
            container_statuses = status.get('containerStatuses', [])
            if container_statuses:
                for pod_item in container_statuses:
                    if pod_item['name'] == 'POD':
                        continue
                    for container in pod.containers:
                        if container['name'] == pod_item['name']:
                            state, state_details = pod_item.pop(
                                'state').items()[0]
                            pod_item['state'] = state
                            pod_item['startedAt'] = state_details.get(
                                'startedAt')
                            if state == 'terminated':
                                pod_item['exitCode'] = state_details.get(
                                    'exitCode')
                                pod_item['finishedAt'] = state_details.get(
                                    'finishedAt')
                            container_id = pod_item.get(
                                'containerID',
                                container['name'])
                            pod_item['containerID'] = _del_docker_prefix(
                                container_id)
                            image_id = pod_item.get(
                                'imageID',
                                container['image'])
                            pod_item['imageID'] = _del_docker_prefix(image_id)
                            container.update(pod_item)
            else:
                for container in pod.containers:
                    container['state'] = pod.status
                    container['containerID'] = None
                    container['imageID'] = None
        else:
            pod.forge_dockers(status=pod.status)

        pod.ready = all(container.get('ready')
                        for container in pod.containers)
        return pod

    def dump(self):
        """Get full information about pod.

        ATTENTION! Do not use it in methods allowed for user! It may contain
        secret information. FOR ADMINS ONLY!
        """
        if self.owner.is_internal():
            raise ServicePodDumpError

        pod_data = self.as_dict()
        owner = self.owner
        k8s_secrets = self.get_secrets()
        volumes_map = self.get_volumes()

        rv = {
            'pod_data': pod_data,
            'owner': owner,
            'k8s_secrets': k8s_secrets,
            'volumes_map': volumes_map,
        }

        return rv

    def get_volumes(self):
        sys_vol = [
            '/usr/lib/kdtools',
        ]
        volumes = getattr(self, 'volumes', [])
        p_vols = (vol for vol in volumes
                  if 'name' in vol and 'hostPath' in vol
                  and vol['hostPath']['path'] not in sys_vol)
        result = {vol['name']: vol['hostPath']['path'] for vol in p_vols}
        ceph_vols = (vol for vol in volumes if 'rbd' in vol)
        result.update({vol['name']: 'ceph' for vol in ceph_vols})
        return result

    def get_secrets(self):
        """Retrieve secrets of type '.dockercfg' from kubernetes.

        Returns dict {secret_name: parsed_secret_data, ...}.
        Structure of parsed_secret_data see in :class:`K8sSecretsBuilder`.
        """
        secrets_client = K8sSecretsClient(KubeQuery())

        try:
            resp = secrets_client.list(namespace=self.namespace)
        except secrets_client.ErrorBase as e:
            raise APIError('Cannot get k8s secrets due to: %s' % e.message)

        parse = K8sSecretsBuilder.parse_secret_data

        rv = {x['metadata']['name']: parse(x['data'])
              for x in resp['items']
              if x['type'] == K8sSecretsClient.SECRET_TYPE}

        return rv

    def _as_dict(self):
        data = vars(self).copy()

        data['volumes'] = data.pop('volumes_public', [])
        for container in data.get('containers', ()):
            new_volumes = []
            for volume_mount in container.get('volumeMounts', ()):
                mount_path = volume_mount.get('mountPath', '')
                # Skip origin root mountPath
                hidden_volumes = [
                    ORIGIN_ROOT, MOUNT_KDTOOLS_PATH, SERVICE_ACCOUNT_STUB_PATH]
                if any(item in mount_path for item in hidden_volumes):
                    continue
                # strip Z option from mountPath
                if mount_path[-2:] in (':Z', ':z'):
                    volume_mount['mountPath'] = mount_path[:-2]
                new_volumes.append(volume_mount)
            container['volumeMounts'] = new_volumes

            # Filter internal variables
            container['env'] = [var for var in container.get('env', [])
                                if var['name'] not in ('KUBERDOCK_SERVICE',)]

        if data.get('edited_config') is not None:
            data['edited_config'] = Pod(data['edited_config']).as_dict()

        return data

    def as_dict(self):
        # unneeded fields in API output
        hide_fields = ['node', 'labels', 'namespace', 'secrets', 'owner']

        data = self._as_dict()

        for field in hide_fields:
            if field in data:
                del data[field]

        return data

    def as_json(self):
        return json.dumps(self.as_dict())

    def compose_persistent(self, reuse_pv=True):
        if not getattr(self, 'volumes', False):
            self.volumes_public = []
            return
        # volumes - k8s api, volumes_public - kd api
        self.volumes_public = deepcopy(self.volumes)
        clean_vols = set()
        for volume, volume_public in zip(self.volumes, self.volumes_public):
            if 'persistentDisk' in volume:
                self._handle_persistent_storage(
                    volume, volume_public, reuse_pv)
            elif 'localStorage' in volume:
                self._handle_local_storage(volume)
            else:
                name = volume.get('name', None)
                clean_vols.add(name)
        if clean_vols:
            self.volumes = [item for item in self.volumes
                            if item['name'] not in clean_vols]

    def _handle_persistent_storage(self, volume, volume_public, reuse_pv):
        """Prepare volume with persistent storage.

        :param volume: volume for k8s api
            (storage specific attributes will be added).
        :param volume_public: volume for kuberdock api
            (all missing fields will be filled).
        :param reuse_pv: if True then reuse existed persistent volumes,
            otherwise raise VolumeExists on name conflict.
        """
        pd = volume.pop('persistentDisk')
        name = pd.get('pdName')

        persistent_disk = PersistentDisk.filter_by(owner_id=self.owner.id,
                                                   name=name).first()
        if persistent_disk is None:
            persistent_disk = PersistentDisk(name=name, owner_id=self.owner.id,
                                             size=pd.get('pdSize', 1))
            db.session.add(persistent_disk)
        else:
            if persistent_disk.state == PersistentDiskStatuses.DELETED:
                persistent_disk.size = pd.get('pdSize', 1)
            elif not reuse_pv:
                raise VolumeExists(persistent_disk.name, persistent_disk.id)
            persistent_disk.state = PersistentDiskStatuses.PENDING
        if volume_public['persistentDisk'].get('pdSize') is None:
            volume_public['persistentDisk']['pdSize'] = persistent_disk.size
        pstorage.STORAGE_CLASS().enrich_volume_info(volume, persistent_disk)

    def _handle_local_storage(self, volume):
        # TODO: cleanup localStorage volumes. It is now used only for pods of
        # internal user.
        local_storage = volume.pop('localStorage')
        if not local_storage:
            return
        if isinstance(local_storage, dict) and 'path' in local_storage:
            path = local_storage['path']
        else:
            path = os.path.join(settings.NODE_LOCAL_STORAGE_PREFIX, self.id,
                                volume['name'])
        volume['hostPath'] = {'path': path}

    # We can't use pod's ports from spec because we strip hostPort from them
    def _dump_ports(self):
        return json.dumps([c.get('ports', []) for c in self.containers])

    def _dump_kubes(self):
        return json.dumps(
            {c.get('name'): c.get('kubes', 1) for c in self.containers})

    @staticmethod
    def extract_volume_annotations(volumes):
        if not volumes:
            return []
        res = [vol.pop('annotation') for vol in volumes if 'annotation' in vol]
        return res

    def prepare(self):
        kube_type = getattr(self, 'kube_type', Kube.get_default_kube_type())
        volumes = getattr(self, 'volumes', [])
        secrets = getattr(self, 'secrets', [])
        volume_annotations = self.extract_volume_annotations(volumes)
        service_account = getattr(self, 'serviceAccount', False)
        service = getattr(self, 'service', '')

        # Extract volumeMounts for missing volumes
        # missing volumes may exist if there some 'Container' storages, as
        # described in https://cloudlinux.atlassian.net/browse/AC-2492
        existing_vols = {item['name'] for item in volumes}
        containers = []
        for container in self.containers:
            container = deepcopy(container)
            container['volumeMounts'] = [
                item for item in container.get('volumeMounts', [])
                if item['name'] in existing_vols]
            containers.append(self._prepare_container(container, kube_type))
        add_kdtools(containers, volumes)
        add_kdenvs(containers, [("KUBERDOCK_SERVICE", service), ])
        if not service_account:
            add_serviceaccount_stub(containers, volumes)

        config = {
            "kind": "ReplicationController",
            "apiVersion": settings.KUBE_API_VERSION,
            "metadata": {
                "name": self.sid,
                "namespace": self.namespace,
                "labels": {
                    "kuberdock-pod-uid": self.id
                }
            },
            "spec": {
                "replicas": getattr(self, 'replicas', 1),
                "selector": {
                    "kuberdock-pod-uid": self.id
                },
                "template": {
                    "metadata": {
                        "labels": {
                            "kuberdock-pod-uid": self.id,
                            "kuberdock-user-uid": str(self.owner.id),
                        },
                        "annotations": {
                            "kuberdock-pod-ports": self._dump_ports(),
                            "kuberdock-container-kubes": self._dump_kubes(),
                            "kuberdock-volume-annotations": json.dumps(
                                volume_annotations
                            ),
                            "kuberdock-volumes-to-prefill":
                                json.dumps(self._get_volumes_to_prefill())
                        }
                    },
                    "spec": {
                        "securityContext": {'seLinuxOptions': {}},
                        "volumes": volumes,
                        "containers": containers,
                        "restartPolicy": getattr(self, 'restartPolicy',
                                                 'Always'),
                        "dnsPolicy": getattr(self, 'dnsPolicy',
                                             'ClusterFirst'),
                        "hostNetwork": getattr(self, 'hostNetwork', False),
                        "imagePullSecrets": [{"name": secret}
                                             for secret in secrets]
                    }
                }
            }
        }
        pod_config = config['spec']['template']
        self._merge_pod_labels(pod_config)

        # Internal services may run on any nodes, do not care of kube type of
        # the node. All other kube types must be binded to the appropriate
        # nodes
        if Kube.is_node_attachable_type(kube_type):
            pod_config['spec']['nodeSelector'] = {
                "kuberdock-kube-type": "type_{0}".format(kube_type)
            }
        else:
            pod_config['spec']['nodeSelector'] = {}

        config_values = {
            'node': 'spec.nodeSelector.kuberdock-node-hostname',
            'public_ip': 'metadata.labels.kuberdock-public-ip',
            'domain': 'metadata.labels.kuberdock-domain',
            'custom_domain': 'metadata.labels.kuberdock-custom-domain',
        }

        for key, path in config_values.items():
            value = getattr(self, key, None)
            if value:
                nested_dict_utils.set(pod_config, path, value)

        return config

    def _merge_pod_labels(self, config):
        for k, v in getattr(self, 'labels', {}).items():
            config['metadata']['labels'][k] = v

    def _get_volumes_to_prefill(self):
        return [
            v['name']
            for c in self.containers
            for v in c.get('volumeMounts', [])
            if v.pop('kdCopyFromImage', False)
            ]

    def _update_volume_path(self, name, vid):
        if vid is None:
            return
        for vol in getattr(self, 'volumes', []):
            if vol.get('name') != name:
                continue
            try:
                vol['awsElasticBlockStore']['volumeID'] += vid
            except KeyError:
                continue

    def _prepare_container(self, data, kube_type=None):
        data = deepcopy(data)
        # Strip non-kubernetes params
        data.pop('sourceUrl', None)

        if kube_type is None:
            kube_type = Kube.get_default_kube_type()

        if not data.get('name'):
            data['name'] = podutils.make_name_from_image(data.get('image', ''))

        try:
            kubes = int(data.pop('kubes'))
        except (KeyError, ValueError):
            pass
        else:  # if we create pod, not start stopped
            data.update(billing.kubes_to_limits(kubes, kube_type))

        wd = data.get('workingDir', '.')
        if type(wd) is list:
            data['workingDir'] = ','.join(data['workingDir'])

        for p in data.get('ports', []):
            p['protocol'] = p.get('protocol', 'TCP').upper()
            p.pop('isPublic', None)  # Non-kubernetes param

        if isinstance(self.owner, basestring):
            current_app.logger.warning('Pod owner field is a string type - '
                                       'possibly refactoring problem')
            owner_name = self.owner
        else:
            owner_name = self.owner.username
        if owner_name != settings.KUBERDOCK_INTERNAL_USER:
            for p in data.get('ports', []):
                p.pop('hostPort', None)

        data['imagePullPolicy'] = 'Always'
        return data

    def _parse_cmd_string(self, cmd_string):
        lex = shlex.shlex(cmd_string, posix=True)
        lex.whitespace_split = True
        lex.commenters = ''
        lex.wordchars += '.'
        try:
            return list(lex)
        except ValueError:
            podutils.raise_('Incorrect cmd string')

    def forge_dockers(self, status='stopped'):
        for container in self.containers:
            container.update({
                'containerID': container['name'],
                'imageID': container['image'],
                'lastState': {},
                'ready': False,
                'restartCount': 0,
                'state': status,
                'startedAt': None,
            })

    def check_name(self):
        DBPod.check_name(self.name, self.owner.id, self.id)

    def set_status(self, status, send_update=False, force=False):
        """Updates pod status in database"""
        # We can't be sure the attribute is already assigned, because
        # attributes of Pod class not defined in __init__.
        # For example attr 'status' will not be defined if we just
        # create Pod object from db config of model Pod.
        if getattr(self, 'status', None) == POD_STATUSES.unpaid and not force:
            # TODO: remove  status "unpaid", use separate field/flag,
            # then remove this block
            raise APIError('Not allowed to change "unpaid" status.',
                           type='NotAllowedToChangeUnpaidStatus')

        db_pod = DBPod.query.get(self.id)
        if not db_pod:
            raise APIError('Pod {} does not exist in KuberDock '
                           'database'.format(self.id))
        # We shouldn't change pod's deleted status.
        if db_pod.status == POD_STATUSES.deleted:
            raise APIError('Not allowed to change "deleted" status.',
                           type='NotAllowedToChangeDeletedStatus')

        helpers.set_pod_status(self.id, status, send_update=send_update)
        self.status = status

    def __repr__(self):
        name = getattr(self, 'name', '').encode('ascii', 'replace')
        return "<Pod ('id':{0}, 'name':{1})>".format(self.id, name)


def _del_docker_prefix(value):
    """Removes 'docker://' from container or image id returned from kubernetes
    API.

    """
    if not value:
        return value
    return value.split('docker://')[-1]


def add_kdtools(containers, volumes):
    """Adds volume to mount kd tools for every container.
    That tools contains statically linked binaries to provide ssh access
    into containers.

    """
    prefix = 'kdtools-'
    volume_name = prefix + uuid.uuid4().hex
    # Make sure we remove previous info, to handle case when image changes
    kdtools_vol = filter(lambda v: v['name'].startswith(prefix), volumes)
    if kdtools_vol:
        volumes.remove(kdtools_vol[0])
    volumes.append({
        u'hostPath': {u'path': HOST_KDTOOLS_PATH},
        u'name': volume_name})
    for container in containers:
        kdtools_mnt = filter(lambda m: m['name'].startswith(prefix),
                             container['volumeMounts'])
        if kdtools_mnt:
            container['volumeMounts'].remove(kdtools_mnt[0])
        container['volumeMounts'].append({
            u'readOnly': True,
            u'mountPath': MOUNT_KDTOOLS_PATH,
            u'name': volume_name
        })


def add_serviceaccount_stub(containers, volumes):
    """Add Service Account stub for Pods that do not needed it.

    It's a workaround to prevent access from non-service pods to k8s services.
    TODO: Probably there is a better way to do this.
    See: http://kubernetes.io/docs/admin/service-accounts-admin/
    http://kubernetes.io/docs/user-guide/service-accounts/

    :param containers: Pod Containers
    :type containers: list
    :param volumes: Pod Volumes
    :type volumes: list

    TODO: Why do we need this?
    """

    volume_name = 'serviceaccount-stub-' + uuid.uuid4().hex
    volumes.append({
        'emptyDir': {},
        'name': volume_name
    })
    for container in containers:
        container['volumeMounts'].append({
            'mountPath': SERVICE_ACCOUNT_STUB_PATH,
            'name': volume_name
        })


def add_kdenvs(containers, envs):
    """Add KuberDock related Environment Variables to Pod Containers.

    :param containers: Pod Containers
    :type containers: list
    :param envs: Environment Variables to be added
    :type envs: list
    """

    for container in containers:
        env = container.get('env', [])

        # TODO: What if such var already exists in list ? We should have
        # unittests for this function
        for name, value in envs:
            env.insert(0, {'name': name, 'value': value})
        if env:
            container['env'] = env
