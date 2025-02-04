import hashlib
import json
import mimetypes
import os
from zipfile import ZipFile, ZIP_DEFLATED
from copy import deepcopy
from datetime import datetime
from multiprocessing.pool import ThreadPool
from tempfile import mkdtemp, mkstemp
from threading import Thread
from multiprocessing import RLock, Event
from time import time

import humanfriendly
import six
from pathlib2 import Path
from PIL import Image

from ..backend_interface.metrics.events import UploadEvent
from ..backend_api import Session
from ..debugging.log import LoggerRoot
from ..backend_api.services import tasks

try:
    import pandas as pd
except ImportError:
    pd = None
try:
    import numpy as np
except ImportError:
    np = None


class Artifact(object):
    """
    Read-Only Artifact object
    """

    @property
    def url(self):
        """
        :return: url of uploaded artifact
        """
        return self._url

    @property
    def name(self):
        """
        :return: name of artifact
        """
        return self._name

    @property
    def size(self):
        """
        :return: size in bytes of artifact
        """
        return self._size

    @property
    def type(self):
        """
        :return: type (str) of of artifact
        """
        return self._type

    @property
    def mode(self):
        """
        :return: mode (str) of of artifact. either "input" or "output"
        """
        return self._mode

    @property
    def hash(self):
        """
        :return: SHA2 hash (str) of of artifact content.
        """
        return self._hash

    @property
    def timestamp(self):
        """
        :return: Timestamp (datetime) of uploaded artifact.
        """
        return self._timestamp

    @property
    def metadata(self):
        """
        :return: Key/Value dictionary attached to artifact.
        """
        return self._metadata

    @property
    def preview(self):
        """
        :return: string (str) representation of the artifact.
        """
        return self._preview

    def __init__(self, artifact_api_object):
        """
        construct read-only object from api artifact object

        :param tasks.Artifact artifact_api_object:
        """
        self._name = artifact_api_object.key
        self._size = artifact_api_object.content_size
        self._type = artifact_api_object.type
        self._mode = artifact_api_object.mode
        self._url = artifact_api_object.uri
        self._hash = artifact_api_object.hash
        self._timestamp = datetime.fromtimestamp(artifact_api_object.timestamp)
        self._metadata = dict(artifact_api_object.display_data) if artifact_api_object.display_data else {}
        self._preview = artifact_api_object.type_data.preview if artifact_api_object.type_data else None

    def get_local_copy(self, extract_archive=True):
        """
        :param bool extract_archive: If True and artifact is of type 'archive' (compressed folder)
            The returned path will be a temporary folder containing the archive content
        :return: a local path to a downloaded copy of the artifact
        """
        from trains.storage.helper import StorageHelper
        local_path = StorageHelper.get_local_copy(self.url)
        if local_path and extract_archive and self.type == 'archive':
            try:
                temp_folder = mkdtemp(prefix='artifact_', suffix='.archive_'+self.name)
                ZipFile(local_path).extractall(path=temp_folder)
            except Exception:
                try:
                    Path(temp_folder).rmdir()
                except Exception:
                    pass
                return local_path
            try:
                Path(local_path).unlink()
            except Exception:
                pass
            return temp_folder

        return local_path

    def __repr__(self):
        return str({'name': self.name, 'size': self.size, 'type': self.type, 'mode': self.mode, 'url': self.url,
                    'hash': self.hash, 'timestamp': self.timestamp,
                    'metadata': self.metadata, 'preview': self.preview, })


class Artifacts(object):
    _flush_frequency_sec = 300.
    # notice these two should match
    _save_format = '.csv.gz'
    _compression = 'gzip'
    # hashing constants
    _hash_block_size = 65536
    _pd_artifact_type = 'data-audit-table'

    class _ProxyDictWrite(dict):
        """ Dictionary wrapper that updates an arguments instance on any item set in the dictionary """
        def __init__(self, artifacts_manager, *args, **kwargs):
            super(Artifacts._ProxyDictWrite, self).__init__(*args, **kwargs)
            self._artifacts_manager = artifacts_manager
            # list of artifacts we should not upload (by name & weak-reference)
            self.artifact_metadata = {}

        def __setitem__(self, key, value):
            # check that value is of type pandas
            if pd and isinstance(value, pd.DataFrame):
                super(Artifacts._ProxyDictWrite, self).__setitem__(key, value)

                if self._artifacts_manager:
                    self._artifacts_manager.flush()
            else:
                raise ValueError('Artifacts currently support pandas.DataFrame objects only')

        def unregister_artifact(self, name):
            self.artifact_metadata.pop(name, None)
            self.pop(name, None)

        def add_metadata(self, name, metadata):
            self.artifact_metadata[name] = deepcopy(metadata)

        def get_metadata(self, name):
            return self.artifact_metadata.get(name)

    @property
    def registered_artifacts(self):
        return self._artifacts_dict

    @property
    def summary(self):
        return self._summary

    def __init__(self, task):
        self._task = task
        # notice the double link, this important since the Artifact
        # dictionary needs to signal the Artifacts base on changes
        self._artifacts_dict = self._ProxyDictWrite(self)
        self._last_artifacts_upload = {}
        self._unregister_request = set()
        self._thread = None
        self._flush_event = Event()
        self._exit_flag = False
        self._thread_pool = ThreadPool()
        self._summary = ''
        self._temp_folder = []
        self._task_artifact_list = []
        self._task_edit_lock = RLock()
        self._storage_prefix = None

    def register_artifact(self, name, artifact, metadata=None):
        # currently we support pandas.DataFrame (which we will upload as csv.gz)
        if name in self._artifacts_dict:
            LoggerRoot.get_base_logger().info('Register artifact, overwriting existing artifact \"{}\"'.format(name))
        self._artifacts_dict[name] = artifact
        if metadata:
            self._artifacts_dict.add_metadata(name, metadata)

    def unregister_artifact(self, name):
        # Remove artifact from the watch list
        self._unregister_request.add(name)
        self.flush()

    def upload_artifact(self, name, artifact_object=None, metadata=None, delete_after_upload=False):
        if not Session.check_min_api_version('2.3'):
            LoggerRoot.get_base_logger().warning('Artifacts not supported by your TRAINS-server version, '
                                                 'please upgrade to the latest server version')
            return False

        if name in self._artifacts_dict:
            raise ValueError("Artifact by the name of {} is already registered, use register_artifact".format(name))

        artifact_type_data = tasks.ArtifactTypeData()
        override_filename_in_uri = None
        override_filename_ext_in_uri = None
        if np and isinstance(artifact_object, np.ndarray):
            artifact_type = 'numpy'
            artifact_type_data.content_type = 'application/numpy'
            artifact_type_data.preview = str(artifact_object.__repr__())
            override_filename_ext_in_uri = '.npz'
            override_filename_in_uri = name+override_filename_ext_in_uri
            fd, local_filename = mkstemp(prefix=name+'.', suffix=override_filename_ext_in_uri)
            os.close(fd)
            np.savez_compressed(local_filename, **{name: artifact_object})
            delete_after_upload = True
        elif pd and isinstance(artifact_object, pd.DataFrame):
            artifact_type = 'pandas'
            artifact_type_data.content_type = 'text/csv'
            artifact_type_data.preview = str(artifact_object.__repr__())
            override_filename_ext_in_uri = self._save_format
            override_filename_in_uri = name
            fd, local_filename = mkstemp(prefix=name+'.', suffix=override_filename_ext_in_uri)
            os.close(fd)
            artifact_object.to_csv(local_filename, compression=self._compression)
            delete_after_upload = True
        elif isinstance(artifact_object, Image.Image):
            artifact_type = 'image'
            artifact_type_data.content_type = 'image/png'
            desc = str(artifact_object.__repr__())
            artifact_type_data.preview = desc[1:desc.find(' at ')]
            override_filename_ext_in_uri = '.png'
            override_filename_in_uri = name + override_filename_ext_in_uri
            fd, local_filename = mkstemp(prefix=name+'.', suffix=override_filename_ext_in_uri)
            os.close(fd)
            artifact_object.save(local_filename)
            delete_after_upload = True
        elif isinstance(artifact_object, dict):
            artifact_type = 'JSON'
            artifact_type_data.content_type = 'application/json'
            preview = json.dumps(artifact_object, sort_keys=True, indent=4)
            override_filename_ext_in_uri = '.json'
            override_filename_in_uri = name + override_filename_ext_in_uri
            fd, local_filename = mkstemp(prefix=name+'.', suffix=override_filename_ext_in_uri)
            os.write(fd, bytes(preview.encode()))
            os.close(fd)
            artifact_type_data.preview = preview
            delete_after_upload = True
        elif isinstance(artifact_object, six.string_types) or isinstance(artifact_object, Path):
            # check if single file
            if isinstance(artifact_object, six.string_types):
                artifact_object = Path(artifact_object)

            artifact_object.expanduser().absolute()
            try:
                create_zip_file = not artifact_object.is_file()
            except Exception:  # Hack for windows pathlib2 bug, is_file isn't valid.
                create_zip_file = True
            else:  # We assume that this is not Windows os
                if artifact_object.is_dir():
                    # change to wildcard
                    artifact_object /= '*'

            if create_zip_file:
                folder = Path('').joinpath(*artifact_object.parts[:-1])
                if not folder.is_dir():
                    raise ValueError("Artifact file/folder '{}' could not be found".format(
                        artifact_object.as_posix()))

                wildcard = artifact_object.parts[-1]
                files = list(Path(folder).rglob(wildcard))
                override_filename_ext_in_uri = '.zip'
                override_filename_in_uri = folder.parts[-1] + override_filename_ext_in_uri
                fd, zip_file = mkstemp(prefix=folder.parts[-1]+'.', suffix=override_filename_ext_in_uri)
                try:
                    artifact_type_data.content_type = 'application/zip'
                    artifact_type_data.preview = 'Archive content {}:\n'.format(artifact_object.as_posix())

                    with ZipFile(zip_file, 'w', allowZip64=True, compression=ZIP_DEFLATED) as zf:
                        for filename in sorted(files):
                            if filename.is_file():
                                relative_file_name = filename.relative_to(folder).as_posix()
                                artifact_type_data.preview += '{} - {}\n'.format(
                                    relative_file_name, humanfriendly.format_size(filename.stat().st_size))
                                zf.write(filename.as_posix(), arcname=relative_file_name)
                except Exception as e:
                    # failed uploading folder:
                    LoggerRoot.get_base_logger().warning('Exception {}\nFailed zipping artifact folder {}'.format(
                        folder, e))
                    return None
                finally:
                    os.close(fd)

                artifact_object = zip_file
                artifact_type = 'archive'
                artifact_type_data.content_type = mimetypes.guess_type(artifact_object)[0]
                local_filename = artifact_object
                delete_after_upload = True
            else:
                if not artifact_object.is_file():
                    raise ValueError("Artifact file '{}' could not be found".format(artifact_object.as_posix()))

                override_filename_in_uri = artifact_object.parts[-1]
                artifact_object = artifact_object.as_posix()
                artifact_type = 'custom'
                artifact_type_data.content_type = mimetypes.guess_type(artifact_object)[0]
                local_filename = artifact_object
        else:
            raise ValueError("Artifact type {} not supported".format(type(artifact_object)))

        # remove from existing list, if exists
        for artifact in self._task_artifact_list:
            if artifact.key == name:
                if artifact.type == self._pd_artifact_type:
                    raise ValueError("Artifact of name {} already registered, "
                                     "use register_artifact instead".format(name))

                self._task_artifact_list.remove(artifact)
                break

        # check that the file to upload exists
        local_filename = Path(local_filename).absolute()
        if not local_filename.exists() or not local_filename.is_file():
            LoggerRoot.get_base_logger().warning('Artifact upload failed, cannot find file {}'.format(
                local_filename.as_posix()))
            return False

        file_hash, _ = self.sha256sum(local_filename.as_posix())
        timestamp = int(time())
        file_size = local_filename.stat().st_size

        uri = self._upload_local_file(local_filename, name,
                                      delete_after_upload=delete_after_upload,
                                      override_filename=override_filename_in_uri,
                                      override_filename_ext=override_filename_ext_in_uri)

        artifact = tasks.Artifact(key=name, type=artifact_type,
                                  uri=uri,
                                  content_size=file_size,
                                  hash=file_hash,
                                  timestamp=timestamp,
                                  type_data=artifact_type_data,
                                  display_data=[(str(k), str(v)) for k, v in metadata.items()] if metadata else None)

        # update task artifacts
        with self._task_edit_lock:
            self._task_artifact_list.append(artifact)
            self._task.set_artifacts(self._task_artifact_list)

        return True

    def flush(self):
        # start the thread if it hasn't already:
        self._start()
        # flush the current state of all artifacts
        self._flush_event.set()

    def stop(self, wait=True):
        # stop the daemon thread and quit
        # wait until thread exists
        self._exit_flag = True
        self._flush_event.set()
        if wait:
            if self._thread:
                self._thread.join()
            # remove all temp folders
            for f in self._temp_folder:
                try:
                    Path(f).rmdir()
                except Exception:
                    pass

    def _start(self):
        if not self._thread:
            # start the daemon thread
            self._flush_event.clear()
            self._thread = Thread(target=self._daemon)
            self._thread.daemon = True
            self._thread.start()

    def _daemon(self):
        while not self._exit_flag:
            self._flush_event.wait(self._flush_frequency_sec)
            self._flush_event.clear()
            artifact_keys = list(self._artifacts_dict.keys())
            for name in artifact_keys:
                try:
                    self._upload_data_audit_artifacts(name)
                except Exception as e:
                    LoggerRoot.get_base_logger().warning(str(e))

        # create summary
        self._summary = self._get_statistics()

    def _upload_data_audit_artifacts(self, name):
        logger = self._task.get_logger()
        pd_artifact = self._artifacts_dict.get(name)
        pd_metadata = self._artifacts_dict.get_metadata(name)

        # remove from artifacts watch list
        if name in self._unregister_request:
            try:
                self._unregister_request.remove(name)
            except KeyError:
                pass
            self._artifacts_dict.unregister_artifact(name)

        if pd_artifact is None:
            return

        override_filename_ext_in_uri = self._save_format
        override_filename_in_uri = name
        fd, local_csv = mkstemp(prefix=name + '.', suffix=override_filename_ext_in_uri)
        os.close(fd)
        local_csv = Path(local_csv)
        pd_artifact.to_csv(local_csv.as_posix(), index=False, compression=self._compression)
        current_sha2, file_sha2 = self.sha256sum(local_csv.as_posix(), skip_header=32)
        if name in self._last_artifacts_upload:
            previous_sha2 = self._last_artifacts_upload[name]
            if previous_sha2 == current_sha2:
                # nothing to do, we can skip the upload
                try:
                    local_csv.unlink()
                except Exception:
                    pass
                return
        self._last_artifacts_upload[name] = current_sha2

        # If old trains-server, upload as debug image
        if not Session.check_min_api_version('2.3'):
            logger.report_image(title='artifacts', series=name, local_path=local_csv.as_posix(),
                                delete_after_upload=True, iteration=self._task.get_last_iteration(),
                                max_image_history=2)
            return

        # Find our artifact
        artifact = None
        for an_artifact in self._task_artifact_list:
            if an_artifact.key == name:
                artifact = an_artifact
                break

        file_size = local_csv.stat().st_size

        # upload file
        uri = self._upload_local_file(local_csv, name, delete_after_upload=True,
                                      override_filename=override_filename_in_uri,
                                      override_filename_ext=override_filename_ext_in_uri)

        # update task artifacts
        with self._task_edit_lock:
            if not artifact:
                artifact = tasks.Artifact(key=name, type=self._pd_artifact_type)
                self._task_artifact_list.append(artifact)
            artifact_type_data = tasks.ArtifactTypeData()

            artifact_type_data.data_hash = current_sha2
            artifact_type_data.content_type = "text/csv"
            artifact_type_data.preview = str(pd_artifact.__repr__())+'\n\n'+self._get_statistics({name: pd_artifact})

            artifact.type_data = artifact_type_data
            artifact.uri = uri
            artifact.content_size = file_size
            artifact.hash = file_sha2
            artifact.timestamp = int(time())
            artifact.display_data = [(str(k), str(v)) for k, v in pd_metadata.items()] if pd_metadata else None

            self._task.set_artifacts(self._task_artifact_list)

    def _upload_local_file(self, local_file, name, delete_after_upload=False,
                           override_filename=None,
                           override_filename_ext=None):
        """
        Upload local file and return uri of the uploaded file (uploading in the background)
        """
        upload_uri = self._task.output_uri or self._task.get_logger().get_default_upload_destination()
        if not isinstance(local_file, Path):
            local_file = Path(local_file)
        ev = UploadEvent(metric='artifacts', variant=name,
                         image_data=None, upload_uri=upload_uri,
                         local_image_path=local_file.as_posix(),
                         delete_after_upload=delete_after_upload,
                         override_filename=override_filename,
                         override_filename_ext=override_filename_ext,
                         override_storage_key_prefix=self._get_storage_uri_prefix())
        _, uri = ev.get_target_full_upload_uri(upload_uri)

        # send for upload
        self._task.reporter._report(ev)

        return uri

    def _get_statistics(self, artifacts_dict=None):
        summary = ''
        artifacts_dict = artifacts_dict or self._artifacts_dict
        thread_pool = ThreadPool()

        try:
            # build hash row sets
            artifacts_summary = []
            for a_name, a_df in artifacts_dict.items():
                if not pd or not isinstance(a_df, pd.DataFrame):
                    continue

                a_unique_hash = set()

                def hash_row(r):
                    a_unique_hash.add(hash(bytes(r)))

                a_shape = a_df.shape
                # parallelize
                thread_pool.map(hash_row, a_df.values)
                # add result
                artifacts_summary.append((a_name, a_shape, a_unique_hash,))

            # build intersection summary
            for i, (name, shape, unique_hash) in enumerate(artifacts_summary):
                summary += '[{name}]: shape={shape}, {unique} unique rows, {percentage:.1f}% uniqueness\n'.format(
                    name=name, shape=shape, unique=len(unique_hash), percentage=100*len(unique_hash)/float(shape[0]))
                for name2, shape2, unique_hash2 in artifacts_summary[i+1:]:
                    intersection = len(unique_hash & unique_hash2)
                    summary += '\tIntersection with [{name2}] {intersection} rows: {percentage:.1f}%\n'.format(
                        name2=name2, intersection=intersection, percentage=100*intersection/float(len(unique_hash2)))
        except Exception as e:
            LoggerRoot.get_base_logger().warning(str(e))
        finally:
            thread_pool.close()
            thread_pool.terminate()
        return summary

    def _get_temp_folder(self, force_new=False):
        if force_new or not self._temp_folder:
            new_temp = mkdtemp(prefix='artifacts_')
            self._temp_folder.append(new_temp)
            return new_temp
        return self._temp_folder[0]

    def _get_storage_uri_prefix(self):
        if not self._storage_prefix:
            self._storage_prefix = self._task._get_output_destination_suffix()
        return self._storage_prefix

    @staticmethod
    def sha256sum(filename, skip_header=0):
        # create sha2 of the file, notice we skip the header of the file (32 bytes)
        # because sometimes that is the only change
        h = hashlib.sha256()
        file_hash = hashlib.sha256()
        b = bytearray(Artifacts._hash_block_size)
        mv = memoryview(b)
        try:
            with open(filename, 'rb', buffering=0) as f:
                # skip header
                if skip_header:
                    file_hash.update(f.read(skip_header))
                for n in iter(lambda: f.readinto(mv), 0):
                    h.update(mv[:n])
                    if skip_header:
                        file_hash.update(mv[:n])
        except Exception as e:
            LoggerRoot.get_base_logger().warning(str(e))
            return None, None

        return h.hexdigest(), file_hash.hexdigest() if skip_header else None
