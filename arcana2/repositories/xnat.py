import os
import os.path as op
import stat
import typing as ty
from glob import glob
import time
import logging
import errno
import json
import re
from zipfile import ZipFile, BadZipfile
import shutil
import attr
from tqdm import tqdm
import xnat
from arcana2.core.utils import JSON_ENCODING
from arcana2.core.repository import Repository
from arcana2.exceptions import (
    ArcanaError, ArcanaNameError, ArcanaUsageError, ArcanaFileFormatError,
    ArcanaWrongRepositoryError)
from arcana2.core.data.provenance import DataProvenance
from arcana2.core.utils import dir_modtime, get_class_info, parse_value
from arcana2.core.data.set import Dataset
from arcana2.dimensions.clinical import Clinical



logger = logging.getLogger('arcana2')

special_char_re = re.compile(r'[^a-zA-Z_0-9]')
tag_parse_re = re.compile(r'\((\d+),(\d+)\)')

RELEVANT_DICOM_TAG_TYPES = set(('UI', 'CS', 'DA', 'TM', 'SH', 'LO',
                                'PN', 'ST', 'AS'))

COMMAND_INPUT_TYPES = {
    bool: 'bool',
    str: 'string',
    int: 'number',
    float: 'number'}

@attr.s
class Xnat(Repository):
    """
    A 'Repository' class for XNAT repositories

    Parameters
    ----------
    server : str (URI)
        URI of XNAT server to connect to
    project_id : str
        The ID of the project in the XNAT repository
    cache_dir : str (name_path)
        Path to local directory to cache remote data in
    user : str
        Username with which to connect to XNAT with
    password : str
        Password to connect to the XNAT repository with
    check_md5 : bool
        Whether to check the MD5 digest of cached files before using. This
        checks for updates on the server since the file was cached
    race_cond_delay : int
        The amount of time to wait before checking that the required
        file_group has been downloaded to cache by another process has
        completed if they are attempting to download the same file_group
    session_filter : str
        A regular expression that is used to prefilter the discovered sessions
        to avoid having to retrieve metadata for them, and potentially speeding
        up the initialisation of the Analysis. Note that if the processing
        relies on summary derivatives (i.e. of 'per_timepoint/subject/analysis'
        frequency) then the filter should match all sessions in the Analysis's
        subject_ids and timepoint_ids.
    """

    server: str = attr.ib()
    cache_dir: str = attr.ib()
    user: str = attr.ib(default=None)
    password: str = attr.ib(default=None)
    check_md5: bool = attr.ib(default=True)
    race_condition_delay: int = attr.ib(default=30)
    _cached_datasets: ty.Dict[str, Dataset]= attr.ib(factory=dict, init=False)
    _login = attr.ib(default=None, init=False)

    type = 'xnat'
    MD5_SUFFIX = '.md5.json'
    PROV_SUFFIX = '.__prov__.json'
    FIELD_PROV_RESOURCE = '__provenance__'
    depth = 2
    DEFAULT_HIERARCHY = [Clinical.subject, Clinical.session]

    @property
    def prov(self):
        return {
            'type': get_class_info(type(self)),
            'server': self.server}

    @property
    def login(self):
        if self._login is None:
            raise ArcanaError("XNAT repository has been disconnected before "
                              "exiting outer context")
        return self._login

    def dataset_cache_dir(self, dataset_name):
        return op.join(self.cache_dir, dataset_name)

    def connect(self):
        """
        Parameters
        ----------
        prev_login : xnat.XNATSession
            An XNAT login that has been opened in the code that calls
            the method that calls login. It is wrapped in a
            NoExitWrapper so the returned connection can be used
            in a "with" statement in the method.
        """
        sess_kwargs = {}
        if self.user is not None:
            sess_kwargs['user'] = self.user
        if self.password is not None:
            sess_kwargs['password'] = self.password
        self._login = xnat.connect(server=self.server, **sess_kwargs)

    def disconnect(self):
        self.login.disconnect()
        self._login = None

    def get_file_group_paths(self, file_group):
        """
        Caches a file_group to the local file system and returns the path to
        the cached files

        Parameters
        ----------
        file_group : FileGroup
            The file_group to cache

        Returns
        -------
        primary_path : str
            The name_path of the primary file once it has been cached
        side_cars : dict[str, str]
            A dictionary containing a mapping of auxiliary file names to
            name_paths
        """
        if file_group.data_format is None:
            raise ArcanaUsageError(
                "Attempting to download {}, which has not been assigned a "
                "file format (see FileGroup.formatted)".format(file_group))
        self._check_repository(file_group)
        with self:  # Connect to the XNAT repository if haven't already
            xnode = self.get_xnode(file_group.data_node)
            if not file_group.uri:
                base_uri = self.standard_uri(xnode)
                if file_group.derived:
                    xresource = xnode.resources[self.escape_name(file_group)]
                else:
                    # If file_group is a primary 'scan' (rather than a
                    # derivative) we need to get the resource of the scan
                    # instead of the scan
                    xscan = xnode.scans[file_group.name]
                    file_group.id = xscan.id
                    base_uri += '/scans/' + xscan.id
                    xresource = xscan.resources[file_group.data_format_name]
                # Set URI so we can retrieve checksums if required. We ensure we
                # use the resource name instead of its ID in the URI for
                # consistency with other locations where it is set and to keep the
                # cache name_path consistent
                file_group.uri = base_uri + '/resources/' + xresource.label
            cache_path = self.cache_path(file_group)
            need_to_download = True
            if op.exists(cache_path):
                if self.check_md5:
                    try:
                        with open(cache_path + self.MD5_SUFFIX, 'r') as f:
                            cached_checksums = json.load(f)
                    except IOError:
                        pass
                    else:
                        if cached_checksums == file_group.checksums:
                            need_to_download = False
                else:
                    need_to_download = False
            if need_to_download:
                # The name_path to the directory which the files will be
                # downloaded to.
                tmp_dir = cache_path + '.download'
                xresource = self.login.classes.Resource(uri=file_group.uri,
                                                        xnat_session=self.login)
                try:
                    # Attempt to make tmp download directory. This will
                    # fail if another process (or previous attempt) has
                    # already created it. In that case this process will
                    # wait to see if that download finishes successfully,
                    # and if so use the cached version.
                    os.makedirs(tmp_dir)
                except OSError as e:
                    if e.errno == errno.EEXIST:
                        # Another process may be concurrently downloading
                        # the same file to the cache. Wait for
                        # 'race_cond_delay' seconds and then check that it
                        # has been completed or assume interrupted and
                        # redownload.
                        # TODO: This should really take into account the
                        # size of the file being downloaded, and then the
                        # user can estimate the download speed for their
                        # repository
                        self._delayed_download(
                            tmp_dir, xresource, file_group, cache_path,
                            delay=self._race_cond_delay)
                    else:
                        raise
                else:
                    self.download_file_group(tmp_dir, xresource, file_group,
                                          cache_path)
                    shutil.rmtree(tmp_dir)
        if not file_group.data_format.directory:
            primary_path, side_cars = file_group.data_format.assort_files(
                op.join(cache_path, f) for f in os.listdir(cache_path))
        else:
            primary_path = cache_path
            side_cars = None
        return primary_path, side_cars

    def get_field_value(self, field):
        """
        Retrieves a fields value

        Parameters
        ----------
        field : Field
            The field to retrieve

        Returns
        -------
        value : float or int or str of list[float] or list[int] or list[str]
            The value of the field
        """
        self._check_repository(field)
        with self:
            xsession = self.get_xnode(field.data_node)
            val = xsession.fields[self.escape_name(field)]
            val = val.replace('&quot;', '"')
            val = parse_value(val)
        return val

    def put_file_group(self, file_group):
        """
        Retrieves a fields value

        Parameters
        ----------
        field : Field
            The field to retrieve

        Returns
        -------
        value : float or int or str of list[float] or list[int] or list[str]
            The value of the field
        """
        if file_group.data_format is None:
            raise ArcanaFileFormatError(
                "Format of {} needs to be set before it is uploaded to {}"
                .format(file_group, self))
        self._check_repository(file_group)
        # Open XNAT session
        with self:
            # Add session for derived scans if not present
            xnode = self.get_xnode(file_group.data_node)
            if not file_group.uri:
                name = self.escape_name(file_group)
                # Set the uri of the file_group
                file_group.uri = '{}/resources/{}'.format(
                    self.standard_uri(xnode), name)
            # Copy file_group to cache
            cache_path = self.cache_path(file_group)
            if os.path.exists(cache_path):
                shutil.rmtree(cache_path)
            os.makedirs(cache_path, stat.S_IRWXU | stat.S_IRWXG)
            if file_group.data_format.directory:
                shutil.copytree(file_group.local_cache, cache_path)
            else:
                # Copy primary file
                shutil.copyfile(file_group.local_cache,
                                op.join(cache_path, file_group.fname))
                # Copy auxiliaries
                for sc_fname, sc_path in file_group.aux_file_fnames_and_paths:
                    shutil.copyfile(sc_path, op.join(cache_path, sc_fname))
            with open(cache_path + self.MD5_SUFFIX, 'w',
                      **JSON_ENCODING) as f:
                json.dump(file_group.calculate_checksums(), f, indent=2)
            if file_group.provenance:
                self.put_provenance(file_group)
            # Delete existing resource (if present)
            try:
                xresource = xnode.resources[name]
            except KeyError:
                pass
            else:
                # Delete existing resource. We could possibly just use the
                # 'overwrite' option of upload but this would leave files in
                # the previous file_group that aren't in the current
                xresource.delete()
            # Create the new resource for the file_group
            xresource = self.login.classes.ResourceCatalog(
                parent=xnode, label=name, format=file_group.data_format_name)
            # Upload the files to the new resource                
            if file_group.data_format.directory:
                for dpath, _, fnames  in os.walk(file_group.local_cache):
                    for fname in fnames:
                        fpath = op.join(dpath, fname)
                        frelpath = op.relpath(fpath, file_group.local_cache)
                        xresource.upload(fpath, frelpath)
            else:
                xresource.upload(file_group.name_path, file_group.fname)
                for sc_fname, sc_path in file_group.aux_file_fnames_and_paths:
                    xresource.upload(sc_path, sc_fname)

    def put_field(self, field):
        self._check_repository(field)
        val = field.value
        if field.array:
            if field.data_format is str:
                val = ['"{}"'.format(v) for v in val]
            val = '[' + ','.join(str(v) for v in val) + ']'
        if field.data_format is str:
            val = '"{}"'.format(val)
        with self:
            xsession = self.get_xnode(field.data_node)
            xsession.fields[self.escape_name(field)] = val
        if field.provenance:
            self.put_provenance(field)

    def put_provenance(self, item):
        xnode = self.get_xnode(item.data_node)
        uri = '{}/resources/{}'.format(self.standard_uri(xnode),
                                       self.PROV_RESOURCE)
        cache_dir = self.cache_path(uri)
        os.makedirs(cache_dir, exist_ok=True)
        fname = self.escape_name(item) + '.json'
        if item.is_field:
            fname = self.FIELD_PROV_PREFIX + fname
        cache_path = op.join(cache_dir, fname)
        item.provenance.save(cache_path)
        # TODO: Should also save digest of prov.json to check to see if it
        #       has been altered remotely. This could be put in a field
        #       to save having to download a file
        try:
            xresource = xnode.resources[self.PROV_RESOURCE]
        except KeyError:
            xresource = self.login.classes.ResourceCatalog(
                parent=xnode, label=self.PROV_RESOURCE,
                format='PROVENANCE')
            # Until XnatPy adds a create_resource to projects, subjects &
            # sessions
            # xresource = xnode.create_resource(format_name)
        xresource.upload(cache_path, fname)

    def get_checksums(self, file_group):
        """
        Downloads the MD5 digests associated with the files in the file-set.
        These are saved with the downloaded files in the cache and used to
        check if the files have been updated on the server

        Parameters
        ----------
        resource : xnat.ResourceCatalog
            The xnat resource
        file_format : FileFormat
            The format of the file_group to get the checksums for. Used to
            determine the primary file within the resource and change the
            corresponding key in the checksums dictionary to '.' to match
            the way it is generated locally by Arcana.
        """
        if file_group.uri is None:
            raise ArcanaUsageError(
                "Can't retrieve checksums as URI has not been set for {}"
                .format(file_group))
        with self:
            checksums = {r['Name']: r['digest']
                         for r in self.login.get_json(file_group.uri + '/files')[
                             'ResultSet']['Result']}
        if not file_group.data_format.directory:
            # Replace the key corresponding to the primary file with '.' to
            # match the way that checksums are created by Arcana
            primary = file_group.data_format.assort_files(checksums.keys())[0]
            checksums['.'] = checksums.pop(primary)
        return checksums

    def construct_tree(self, dataset: Dataset, **kwargs):
        """
        Find all file_groups, fields and provenance provenances within an XNAT
        project and create data tree within dataset

        Parameters
        ----------
        dataset : Dataset
            The dataset to construct
        """
        with self:
            # Get per_dataset level derivatives and fields
            for exp in self.login.projects[dataset.name].experiments.values():
                dataset.add_leaf_node([exp.subject.label, exp.label])

    def populate_items(self, data_node):
        with self:
            xnode = self.get_xnode(data_node)
            # Add scans, fields and resources to data node
            for xscan in xnode.scans.values():
                data_node.add_file_group(
                    path=xscan.type,
                    order=xscan.id,
                    quality=xscan.quality,
                    # Ensure uri uses resource label instead of ID
                    uris={r.label: '/'.join(r.uri.split('/')[:-1] + [r.label])
                          for r in xscan.resources.values()})
            for name, value in xnode.fields.items():
                data_node.add_field(
                    path=name,
                    value=value)
            for xresource in xnode.resources.values():
                data_node.add_file_group(
                    path=xresource.name,
                    uris={xresource.data_format: xresource.uri})
            # self.add_scans_to_node(data_node, xnode)
            # self.add_fields_to_node(data_node, xnode)
            # self.add_resources_to_node(data_node, xnode)

    # def add_scans_to_node(self, data_node: Dataset, session_json: dict,
    #                       session_uri: str, **kwargs):
    #     try:
    #         scans_json = next(
    #             c['items'] for c in session_json['children']
    #             if c['field'] == 'scans/scan')
    #     except StopIteration:
    #         return []
    #     file_groups = []
    #     for scan_json in scans_json:
    #         order = scan_json['data_fields']['ID']
    #         scan_type = scan_json['data_fields'].get('type', '')
    #         scan_quality = scan_json['data_fields'].get('quality', None)
    #         try:
    #             resources_json = next(
    #                 c['items'] for c in scan_json['children']
    #                 if c['field'] == 'file')
    #         except StopIteration:
    #             resources = set()
    #         else:
    #             resources = set(js['data_fields']['label']
    #                             for js in resources_json)
    #         data_node.add_file_group(
    #             name=scan_type, order=order, quality=scan_quality,
    #             resource_uris={
    #                 r: f"{session_uri}/scans/{order}/resources/{r}"
    #                 for r in resources}, **kwargs)
    #     return file_groups

    # def add_fields_to_node(self, data_node, node_json, **kwargs):
    #     try:
    #         fields_json = next(
    #             c['items'] for c in node_json['children']
    #             if c['field'] == 'fields/field')
    #     except StopIteration:
    #         return []
    #     for js in fields_json:
    #         try:
    #             value = js['data_fields']['field']
    #         except KeyError:
    #             continue
    #         value = value.replace('&quot;', '"')
    #         name = js['data_fields']['name']
    #         # field_names = set([(name, None, timepoint_id, frequency)])
    #         # # Potentially add the field twice, once
    #         # # as a field name in its own right (for externally created fields)
    #         # # and second as a field name prefixed by an analysis name. Would
    #         # # ideally have the generated fields (and file_groups) in a separate
    #         # # assessor so there was no chance of a conflict but there should
    #         # # be little harm in having the field referenced twice, the only
    #         # # issue being with pattern matching
    #         # field_names.add(self.unescape_name(name, timepoint_id=timepoint_id,
    #         #                                         frequency=frequency))
    #         # for name, namespace, field_timepoint_id, field_freq in field_names:
    #         name, dn = self._unescape_name_and_get_node(name, data_node)
    #         dn.add_field(name=name, value=value **kwargs)

    # def add_resources_to_node(self, data_node, node_json, node_uri, **kwargs):
    #     try:
    #         resources_json = next(
    #             c['items'] for c in node_json['children']
    #             if c['field'] == 'resources/resource')
    #     except StopIteration:
    #         resources_json = []
    #     provenance_resources = []
    #     for d in resources_json:
    #         label = d['data_fields']['label']
    #         resource_uri = '{}/resources/{}'.format(node_uri, label)
    #         name, dn = self._unescape_name_and_get_node(name, data_node)
    #         format_name = d['data_fields']['format']
    #         if name != self.PROV_RESOURCE:
    #             # Use the timepoint from the derived name if present
    #             dn.add_file_group(
    #                 name, resource_uris={format_name: resource_uri}, **kwargs)
    #         else:
    #             provenance_resources.append((dn, resource_uri))
    #     for dn, uri in provenance_resources:
    #         self.set_provenance(dn, uri)

    # def set_provenance(self, data_node, resource_uri):
    #     # Download provenance JSON files and parse into
    #     # provenances
    #     temp_dir = tempfile.mkdtemp()
    #     try:
    #         with tempfile.TemporaryFile() as temp_zip:
    #             self.login.download_stream(
    #                 resource_uri + '/files', temp_zip, format='zip')
    #             with ZipFile(temp_zip) as zip_file:
    #                 zip_file.extractall(temp_dir)
    #         for base_dir, _, fnames in os.walk(temp_dir):
    #             for fname in fnames:
    #                 if fname.endswith('.json'):
    #                     name_path = fname[:-len('.json')]
    #                     prov = DataProvenance.load(op.join(base_dir,
    #                                                     fname))
    #                     if fname.starts_with(self.FIELD_PROV_PREFIX):
    #                         name_path = name_path[len(self.FIELD_PROV_PREFIX):]
    #                         data_node.field(name_path).provenance = prov
    #                     else:
    #                         data_node.file_group(name_path).provenance = prov
    #     finally:
    #         shutil.rmtree(temp_dir, ignore_errors=True)

    # def _unescape_name_and_get_node(self, name, data_node):
    #     name, frequency, ids = self.unescape_name(name)
    #     if frequency != data_node.frequency:
    #         try:
    #             data_node = data_node.dataset.node(frequency, ids)
    #         except ArcanaNameError:
    #             data_node = data_node.dataset.add_node(frequency, ids)
    #     return name, data_node
        

    # def extract_subject_id(self, xsubject_label):
    #     """
    #     This assumes that the subject ID is prepended with
    #     the project ID.
    #     """
    #     return xsubject_label.split('_')[1]

    # def extract_timepoint_id(self, xsession_label):
    #     """
    #     This assumes that the session ID is preprended
    #     """
    #     return '_'.join(xsession_label.split('_')[2:])

    def dicom_header(self, file_group):
        def convert(val, code):
            if code == 'TM':
                try:
                    val = float(val)
                except ValueError:
                    pass
            elif code == 'CS':
                val = val.split('\\')
            return val
        with self:
            scan_uri = '/' + '/'.join(file_group.uri.split('/')[2:-2])
            response = self.login.get(
                '/REST/services/dicomdump?src='
                + scan_uri).json()['ResultSet']['Result']
        hdr = {tag_parse_re.match(t['tag1']).groups(): convert(t['value'],
                                                               t['vr'])
               for t in response if (tag_parse_re.match(t['tag1'])
                                     and t['vr'] in RELEVANT_DICOM_TAG_TYPES)}
        return hdr

    def download_file_group(self, tmp_dir, xresource, file_group, cache_path):
        # Download resource to zip file
        zip_path = op.join(tmp_dir, 'download.zip')
        with open(zip_path, 'wb') as f:
            xresource.xnat_session.download_stream(
                xresource.uri + '/files', f, format='zip', verbose=True)
        checksums = self.get_checksums(file_group)
        # Extract downloaded zip file
        expanded_dir = op.join(tmp_dir, 'expanded')
        try:
            with ZipFile(zip_path) as zip_file:
                zip_file.extractall(expanded_dir)
        except BadZipfile as e:
            raise ArcanaError(
                "Could not unzip file '{}' ({})"
                .format(xresource.id, e))
        data_path = glob(expanded_dir + '/**/files', recursive=True)[0]
        # Remove existing cache if present
        try:
            shutil.rmtree(cache_path)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise e
        shutil.move(data_path, cache_path)
        with open(cache_path + self.MD5_SUFFIX, 'w', **JSON_ENCODING) as f:
            json.dump(checksums, f, indent=2)

    def _delayed_download(self, tmp_dir, xresource, file_group, cache_path,
                          delay):
        logger.info("Waiting %s seconds for incomplete download of '%s' "
                    "initiated another process to finish", delay, cache_path)
        initial_mod_time = dir_modtime(tmp_dir)
        time.sleep(delay)
        if op.exists(cache_path):
            logger.info("The download of '%s' has completed "
                        "successfully in the other process, continuing",
                        cache_path)
            return
        elif initial_mod_time != dir_modtime(tmp_dir):
            logger.info(
                "The download of '%s' hasn't completed yet, but it has"
                " been updated.  Waiting another %s seconds before "
                "checking again.", cache_path, delay)
            self._delayed_download(tmp_dir, xresource, file_group, cache_path,
                                   delay)
        else:
            logger.warning(
                "The download of '%s' hasn't updated in %s "
                "seconds, assuming that it was interrupted and "
                "restarting download", cache_path, delay)
            shutil.rmtree(tmp_dir)
            os.mkdir(tmp_dir)
            self.download_file_group(tmp_dir, xresource, file_group, cache_path)

    def get_xnode(self, data_node):
        """
        Returns the XNAT session and cache dir corresponding to the provided
        data_node

        Parameters
        ----------
        data_node : DataNode
            The data_node to get the corresponding XNAT node for
        """
        with self:
            xproject = self.login.projects[data_node.dataset.name]
            if data_node.frequency not in (Clinical.subject,
                                           Clinical.session):
                return xproject
            subj_label = data_node.ids[Clinical.subject]
            try:
                xsubject = xproject.subjects[subj_label]
            except KeyError:
                xsubject = self.login.classes.SubjectData(
                    label=subj_label, parent=xproject)
            if data_node.frequency == Clinical.subject:
                return xsubject
            sess_label = data_node.ids[Clinical.session]
            try:
                xsession = xsubject.experiments[sess_label]
            except KeyError:
                xsession = self.login.classes.MrSessionData(
                    label=sess_label, parent=xsubject)
            return xsession

    def cache_path(self, item):
        """Path to the directory where the item is/should be cached. Note that
        the URI of the item needs to be set beforehand

        Parameters
        ----------
        item : FileGroup | `str`
            The file_group provenance that has been, or will be, cached

        Returns
        -------
        `str`
            The name_path to the directory where the item will be cached
        """
        # Append the URI after /projects as a relative name_path from the base
        # cache directory
        if not isinstance(item, str):
            uri = item.uri
        else:
            uri = item
        if uri is None:
            raise ArcanaError("URI of item needs to be set before cache path")
        return op.join(self.cache_dir, *uri.split('/')[3:])

    def _check_repository(self, item):
        if item.data_node.dataset.repository is not self:
            raise ArcanaWrongRepositoryError(
                "{} is from {} instead of {}".format(
                    item, item.dataset.repository, self))

    @classmethod
    def escape_name(cls, item):
        """Escape the name of an item by prefixing the name of the current
        analysis

        Parameters
        ----------
        item : FileGroup | Provenance
            The item to generate a derived name for

        Returns
        -------
        `str`
            The derived name
        """
        name = '__'.join(item.name_path)
        if item.data_node.frequency not in (Clinical.subject,
                                            Clinical.session):
            name = ('___'
                    + '___'.join(f'{l}__{item.data_node.ids[l]}'
                                 for l in item.data_node.frequency.hierarchy)
                    + '___')
        return name

    @classmethod
    def unescape_name(cls, xname: str):
        """Reverses the escape of an item name by `escape_name`

        Parameters
        ----------
        xname : `str`
            An escaped name of a data node stored in the project resources

        Returns
        -------
        name : `str`
            The unescaped name of an item
        frequency : Clinical
            The frequency of the node
        ids : Dict[Clinical, str]
            A dictionary of IDs for the node
        """
        ids = {}
        id_parts = xname.split('___')[1:-1]
        freq_value = 0b0
        for part in id_parts:
            layer_freq_str, id = part.split('__')
            layer_freq = Clinical[layer_freq_str]
            ids[layer_freq] = id
            freq_value != layer_freq
        frequency = Clinical(freq_value)
        name_path = '/'.join(id_parts[-1].split('__'))
        return name_path, frequency, ids

    @classmethod
    def standard_uri(cls, xnode):
        """Get the URI of the XNAT node (ImageSession | Subject | Project)
        using labels rather than IDs for subject and sessions, e.g

        >>> xnode = repo.login.experiments['MRH017_100_MR01']
        >>> repo.standard_uri(xnode)

        '/data/archive/projects/MRH017/subjects/MRH017_100/experiments/MRH017_100_MR01'

        Parameters
        ----------
        xnode : xnat.ImageSession | xnat.Subject | xnat.Project
            A node of the XNAT data tree
        """
        uri = xnode.uri
        if 'experiments' in uri:
            # Replace ImageSession ID with label in URI.
            uri = re.sub(r'(?<=/experiments/)[^/]+', xnode.label, uri)
        if 'subjects' in uri:
            try:
                # If xnode is a ImageSession
                subject_id = xnode.subject.label
            except AttributeError:
                # If xnode is a Subject
                subject_id = xnode.label
            except KeyError:
                # There is a bug where the subject isn't appeared to be cached
                # so we use this as a workaround
                subject_json = xnode.xnat_session.get_json(
                    xnode.uri.split('/experiments')[0])
                subject_id = subject_json['items'][0]['data_fields']['label']
            # Replace subject ID with subject label in URI
            uri = re.sub(r'(?<=/subjects/)[^/]+', subject_id, uri)

        return uri


    @classmethod
    def make_command_json(cls, image_name, analysis_cls, inputs, outputs,
                          parameters, desc, frequency=Clinical.session,
                          docker_index="https://index.docker.io/v1/"):

        if frequency != Clinical.session:
            raise NotImplementedError(
                "Support for frequencies other than '{}' haven't been "
                "implemented yet".format(frequency))
        try:
            analysis_name, version = image_name.split('/')[1].split(':')
        except (IndexError, ValueError):
            raise ArcanaUsageError(
                "The Docker organisation and tag needs to be provided as part "
                "of the image, e.g. australianimagingservice/dwiqa:0.1")

        cmd_inputs = []
        input_names = []
        for inpt in inputs:
            input_name = inpt if isinstance(inpt, str) else inpt[0]
            input_names.append(input_name)
            spec = analysis_cls.data_spec(input_name)
            desc = spec.desc if spec.desc else ""
            if spec.is_file_group:
                desc = ("Scan match: {} [SCAN_TYPE [ORDER [TAG=VALUE, ...]]]"
                        .format(desc))
            else:
                desc = "Field match: {} [FIELD_NAME]".format(desc)
            cmd_inputs.append({
                "name": input_name,
                "description": desc,
                "type": "string",
                "default-value": "",
                "required": True,
                "user-settable": True,
                "replacement-key": "#{}_INPUT#".format(input_name.upper())})

        for param in parameters:
            spec = analysis_cls.param_spec(param)
            desc = "Parameter: " + spec.desc
            if spec.choices:
                desc += " (choices: {})".format(','.join(spec.choices))

            cmd_inputs.append({
                "name": param,
                "description": desc,
                "type": COMMAND_INPUT_TYPES[spec.data_format],
                "default-value": (spec.default
                                    if spec.default is not None else ""),
                "required": spec.default is None,
                "user-settable": True,
                "replacement-key": "#{}_PARAM#".format(param.upper())})

        cmd_inputs.append(
            {
                "name": "project-id",
                "description": "Project ID",
                "type": "string",
                "required": True,
                "user-settable": False,
                "replacement-key": "#PROJECT_ID#"
            })


        cmdline = (
            "arcana derive /input {cls} {name} {derivs} {inputs} {params}"
            " --scratch /work --repository xnat_cs #PROJECT_URI#"
            .format(
                cls='.'.join((analysis_cls.__module__, analysis_cls.__name__)),
                name=analysis_name,
                derivs=' '.join(outputs),
                inputs=' '.join('-i {} #{}_INPUT#'.format(i, i.upper())
                                for i in input_names),
                params=' '.join('-p {} #{}_PARAM#'.format(p, p.upper())
                                for p in parameters)))

        if frequency == Clinical.session:
            cmd_inputs.append(
                {
                    "name": "session-id",
                    "description": "",
                    "type": "string",
                    "required": True,
                    "user-settable": False,
                    "replacement-key": "#SESSION_ID#"
                })
            cmdline += "#SESSION_ID# --session_ids #SESSION_ID# "

        return {
            "name": analysis_name,
            "description": desc,
            "label": analysis_name,
            "version": version,
            "schema-version": "1.0",
            "image": image_name,
            "index": docker_index,
            "type": "docker",
            "command-line": cmdline,
            "override-entrypoint": True,
            "mounts": [
                {
                    "name": "in",
                    "writable": False,
                    "name_path": "/input"
                },
                {
                    "name": "output",
                    "writable": True,
                    "name_path": "/output"
                },
                {
                    "name": "work",
                    "writable": True,
                    "name_path": "/work"
                }
            ],
            "ports": {},
            "inputs": cmd_inputs,
            "outputs": [
                {
                    "name": "output",
                    "description": "Derivatives",
                    "required": True,
                    "mount": "out",
                    "name_path": None,
                    "glob": None
                },
                {
                    "name": "working",
                    "description": "Working directory",
                    "required": True,
                    "mount": "work",
                    "name_path": None,
                    "glob": None
                }
            ],
            "xnat": [
                {
                    "name": analysis_name,
                    "description": desc,
                    "contexts": ["xnat:imageSessionData"],
                    "external-inputs": [
                        {
                            "name": "session",
                            "description": "Imaging session",
                            "type": "Session",
                            "source": None,
                            "default-value": None,
                            "required": True,
                            "replacement-key": None,
                            "sensitive": None,
                            "provides-value-for-command-input": None,
                            "provides-files-for-command-mount": "in",
                            "via-setup-command": None,
                            "user-settable": None,
                            "load-children": True
                        }
                    ],
                    "derived-inputs": [
                        {
                            "name": "session-id",
                            "type": "string",
                            "required": True,
                            "load-children": True,
                            "derived-from-wrapper-input": "session",
                            "derived-from-xnat-object-property": "id",
                            "provides-value-for-command-input": "session-id"
                        },
                        {
                            "name": "subject",
                            "type": "Subject",
                            "required": True,
                            "user-settable": False,
                            "load-children": True,
                            "derived-from-wrapper-input": "session"
                        },
                        {
                            "name": "project-id",
                            "type": "string",
                            "required": True,
                            "load-children": True,
                            "derived-from-wrapper-input": "subject",
                            "derived-from-xnat-object-property": "id",
                            "provides-value-for-command-input": "subject-id"
                        }
                    ],
                    "output-handlers": [
                        {
                            "name": "output-resource",
                            "accepts-command-output": "output",
                            "via-wrapup-command": None,
                            "as-a-child-of": "session",
                            "type": "Resource",
                            "label": "Derivatives",
                            "format": None
                        },
                        {
                            "name": "working-resource",
                            "accepts-command-output": "working",
                            "via-wrapup-command": None,
                            "as-a-child-of": "session",
                            "type": "Resource",
                            "label": "Work",
                            "format": None
                        }
                    ]
                }
            ]
        }
