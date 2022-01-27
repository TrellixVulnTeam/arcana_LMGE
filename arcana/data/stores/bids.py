import attr
import re
import json
import tempfile
import typing as ty
from dataclasses import dataclass
import shutil
from pathlib import Path
from arcana import __version__
from arcana.__about__ import PACKAGE_NAME, CODE_URL
from pydra import Workflow, mark
from pydra.engine.task import (
    DockerTask, SingularityTask, ShellCommandTask)
from pydra.engine.specs import (
    LazyField, ShellSpec, SpecInfo, DockerSpec, SingularitySpec, ShellOutSpec)
from arcana.core.data.set import Dataset
from arcana.data.types.general import directory
from arcana.data.dimensions.clinical import Clinical
from . import FileSystem
from arcana.exceptions import ArcanaError, ArcanaUsageError, ArcanaEmptyDatasetError
from arcana.core.utils import func_task



@dataclass
class ContainerMetadata():

    type: str = None
    tag: str = None
    uri: str = None

    def to_dict(self):
        dct = {}
        if self.type:
            dct['Type'] = self.type
        if self.tag:
            dct['Tag'] = self.tag
        if self.uri:
            dct['URI'] = self.uri
        return dct

    @classmethod
    def from_dict(cls, dct):
        if dct is None:
            return None
        return ContainerMetadata(
            type=dct.get('Type'),
            tag=dct.get('Tag'),
            uri=dct.get('URI'))

@dataclass
class GeneratorMetadata():

    name: str
    version: str = None
    description: str = None
    code_url: str = None
    container: ContainerMetadata = None

    def to_dict(self):
        dct = {
            'Name': self.name}
        if self.version:
            dct['Version'] = self.version
        if self.description:
            dct['Description'] = self.description
        if self.code_url:
            dct['CodeURL'] = self.code_url
        if self.container:
            dct['Container'] = self.container.to_dict()
        return dct

    @classmethod
    def from_dict(cls, dct):
        return GeneratorMetadata(
            name=dct['Name'],
            version=dct.get('Version'),
            description=dct.get('Description'),
            code_url=dct.get('CodeURL'),
            container=ContainerMetadata.from_dict(dct.get('Container')))


@dataclass
class SourceDatasetMetadata():

    url: str = None
    doi: str = None
    version: str = None

    def to_dict(self):
        dct = {}
        if self.url:
            dct['URL'] = self.url
        if self.doi:
            dct['DOI'] = self.doi
        if self.version:
            dct['Version'] = self.version
        return dct

    @classmethod
    def from_dict(cls, dct):
        if dct is None:
            return None
        return SourceDatasetMetadata(
            url=dct.get('URL'),
            doi=dct.get('DOI'),
            version=dct.get('Version'))


@attr.s
class BidsDataset(Dataset):
    """ A representation of a "dataset" in Brain Imaging Data Structure (BIDS)
    format
    """
    name: str = attr.ib(default='Autogenerated-dataset')
    participants: ty.Dict[str, ty.Dict[str, str]] = attr.ib(factory=dict, repr=False)
    acknowledgements: str = attr.ib(default="Generic BIDS dataset", repr=False)
    authors: ty.List[str] = attr.ib(factory=list, repr=False)
    bids_version: str = attr.ib(default='1.0.1', repr=False)
    doi: str = attr.ib(default=None, repr=False)
    funding: ty.List[str] = attr.ib(factory=list, repr=False)
    bids_type: str = attr.ib(default='derivative', repr=False)
    license: str = attr.ib(default='CC0', repr=False)
    references: ty.List[str] = attr.ib(factory=list)
    how_to_acknowledge: str = attr.ib(default="see licence")
    ethics_approvals: ty.List[str] = attr.ib(factory=list)
    generated_by: ty.List[GeneratorMetadata] = attr.ib(factory=list)
    sources: ty.List[SourceDatasetMetadata] = attr.ib(factory=list)
    readme: str = attr.ib(default=None)

    def add_generator_metadata(self, **kwargs):
        self.generated_by.append(GeneratorMetadata(**kwargs))

    def add_source_metadata(self, **kwargs):
        self.sources.append(SourceDatasetMetadata(**kwargs))

    @classmethod
    def load(cls, path):
        if list(Path(path).glob('**/sub-*/ses-*')):
            hierarchy = [Clinical.subject, Clinical.timepoint]
        else:
            hierarchy = [Clinical.session]    
        dataset = BidsDataset(path, store=BidsFormat(),
                              hierarchy=hierarchy)
        dataset.load_metadata()
        return dataset

    @classmethod
    def create(cls, path, name, subject_ids, session_ids=None,
               readme=None, authors=None, generated_by=None, **kwargs):
        path = Path(path)
        path.mkdir()
        if session_ids is not None:
            hierarchy = [Clinical.subject, Clinical.timepoint]
        else:
            hierarchy = [Clinical.session]
        if generated_by is None:
            generated_by = [
                GeneratorMetadata(
                    name=PACKAGE_NAME,
                    version=__version__,
                    description=f"Empty dataset created by {PACKAGE_NAME}",
                    code_url=CODE_URL)]
        if readme is None:
            readme = "Mock readme\n" * 20
        if authors is None:
            authors = ['Mock A. Author',
                       'Mock B. Author']
        dataset = BidsDataset(
            path,
            store=BidsFormat(),
            hierarchy=hierarchy,
            name=name,
            generated_by=generated_by,
            readme=readme,
            authors=authors,
            **kwargs)
        # Create nodes
        for subject_id in subject_ids:
            if not subject_id.startswith('sub-'):
                subject_id = f'sub-{subject_id}'
            dataset.participants[subject_id] = {}
            if session_ids:
                for session_id in session_ids:
                    if not session_id.startswith('sub-'):
                        session_id = f'ses-{session_id}'
                    node = dataset.add_leaf_node([subject_id, session_id])
                    BidsFormat.absolute_node_path(node).mkdir(parents=True)
            else:
                node = dataset.add_leaf_node([subject_id])
                BidsFormat.absolute_node_path(node).mkdir(parents=True)
        dataset.save_metadata()
        return dataset

    def is_multi_session(self):
        return len(self.hierarchy) > 1

    def save_metadata(self):
        if not self.participants:
            raise ArcanaEmptyDatasetError(
                "Dataset needs at least one participant before the metadata "
                "can be saved")
        dct = {
            'Name': self.name,
            'BIDSVersion': self.bids_version}
        if self.bids_type:
            dct['DatasetType'] = self.bids_type
        if self.license:
            dct['Licence'] = self.license
        if self.authors:
            dct['Authors'] = self.authors
        if self.acknowledgements:
            dct['Acknowledgements'] = self.acknowledgements
        if self.how_to_acknowledge:
            dct['HowToAcknowledge'] = self.how_to_acknowledge
        if self.funding:
            dct['Funding'] = self.funding
        if self.ethics_approvals:
            dct['EthicsApprovals'] = self.ethics_approvals
        if self.references:
            dct['ReferencesAndLinks'] = self.references
        if self.doi:
            dct['DatasetDOI'] = self.doi
        if self.bids_type == 'derivative':
            dct['GeneratedBy'] = [g.to_dict() for g in self.generated_by]
        if self.sources:
            dct['sourceDatasets'] = [d.to_dict() for d in self.sources]
        with open(self.root_dir / 'dataset_description.json', 'w') as f:
            json.dump(dct, f, indent='    ')

        with open(self.root_dir / 'participants.tsv', 'w') as f:
            col_names = list(next(iter(self.participants.values())).keys())
            f.write('\t'.join(['participant_id'] + col_names) + '\n')
            for pcpt_id, pcpt_attrs in self.participants.items():
                f.write('\t'.join(
                    [pcpt_id] + [pcpt_attrs[c] for c in col_names]) + '\n')

        if self.readme is not None:
            with open(self.root_dir / 'README', 'w') as f:
                f.write(self.readme)

    def load_metadata(self):
        description_json_path = (self.root_dir / 'dataset_description.json')
        if not description_json_path.exists():
            raise ArcanaEmptyDatasetError(
                f"Could not find a directory at '{self.id}' containing a "
                "'dataset_description.json' file")
        with open(description_json_path) as f:
            dct = json.load(f)               
        self.name = dct['Name']
        self.bids_version = dct['BIDSVersion']
        self.bids_type = dct.get('DatasetType')
        self.license = dct.get('Licence')
        self.authors = dct.get('Authors', [])
        self.acknowledgements = dct.get('Acknowledgements')
        self.how_to_acknowledge = dct.get('HowToAcknowledge')
        self.funding = dct.get('Funding', [])
        self.ethics_approvals = dct.get('EthicsApprovals', [])
        self.references = dct.get('ReferencesAndLinks', [])
        self.doi = dct.get('DatasetDOI')
        if self.bids_type == 'derivative':
            if 'GeneratedBy' not in dct:
                raise ArcanaError(
                    "'GeneratedBy' field required for 'derivative' type datasets")
            self.generated_by = [GeneratorMetadata.from_dict(d)
                                 for d in dct['GeneratedBy']]
        if 'sourceDatasets' in dct:
            self.sources = [SourceDatasetMetadata.from_dict(d)
                            for d in dct['sourceDatasets']]

        self.participants = {}
        with open(self.root_dir / 'participants.tsv') as f:
            cols = f.readline()[:-1].split('\t')
            while line:= f.readline()[:-1]:
                d = dict(zip(cols, line.split('\t')))
                self.participants[d.pop('participant_id')] = d

        readme_path = self.root_dir / 'README'
        if readme_path.exists():
            with open(readme_path) as f:
                self.readme = f.read()
        else:
            self.readme = None


class BidsFormat(FileSystem):
    """Repository for working with data stored on the file-system in BIDS format 
    """

    def find_nodes(self, dataset: BidsDataset):
        """
        Find all nodes within the dataset stored in the store and
        construct the data tree within the dataset

        Parameters
        ----------
        dataset : Dataset
            The dataset to construct the tree dimensions for
        """

        try:
            dataset.load_metadata()
        except ArcanaEmptyDatasetError:
            return

        for subject_id, participant in dataset.participants.items():
            try:
                explicit_ids = {Clinical.group: participant['group']}
            except KeyError:
                explicit_ids = {}
            if dataset.is_multi_session():
                for sess_id in (dataset.root_dir / subject_id).iterdir():
                    dataset.add_leaf_node([subject_id, sess_id],
                                          explicit_ids=explicit_ids)
            else:
                dataset.add_leaf_node([subject_id],
                                      explicit_ids=explicit_ids)

    def find_items(self, data_node):
        rel_session_path = self.node_path(data_node)
        root_dir = data_node.dataset.root_dir
        session_path = (root_dir / rel_session_path)
        session_path.mkdir(exist_ok=True)
        for modality_dir in session_path.iterdir():
            self.find_items_in_dir(modality_dir, data_node)
        deriv_dir = (root_dir / 'derivatives')
        if deriv_dir.exists():
            for pipeline_dir in deriv_dir.iterdir():
                self.find_items_in_dir(pipeline_dir / rel_session_path,
                                       data_node)        

    def file_group_path(self, file_group):
        dn = file_group.data_node
        fs_path = self.root_dir(dn)
        parts = file_group.path.split('/')
        if parts[-1] == '':
            parts = parts[:-1]
        if parts[0] == 'derivatives':
            if len(parts) < 2:
                raise ArcanaUsageError(
                    f"Derivative paths should have at least 3 parts ({file_group.path}")
            elif len(parts) == 2 and file_group.datatype != directory:
                raise ArcanaUsageError(
                    "Derivative paths with 2 parts must be of type directory "
                    f"({file_group.path}")
            fs_path /= parts[0]
            fs_path /= parts[1]
            parts = parts[2:]
        if parts:  # Often the whole derivatives folder is the output for a BIDS apps
            fs_path /= self.node_path(dn)
            for part in parts[:-1]:
                fs_path /= part
        
            fname = '_'.join(dn.ids[h]
                             for h in dn.dataset.hierarchy) + '_' + parts[-1]
            fs_path /= fname
        if file_group.datatype.extension:
            fs_path = fs_path.with_suffix(file_group.datatype.extension)
        return fs_path

    def fields_json_path(self, field):
        parts = field.path.split('/')
        if parts[0] != 'derivatives':
            assert False, "Non-derivative fields should be taken from participants.tsv"
        return (field.data_node.dataset.root_dir.joinpath(parts[:2])
                / self.node_path(field.data_node) / self.FIELDS_FNAME)

    def get_field_val(self, field):
        data_node = field.data_node
        dataset = data_node.dataset
        if field.name in dataset.participant_attrs:
            val = dataset.participants[data_node.ids[Clinical.subject]]
        else:
            val = super().get_field_val(field)
        return val


def outputs_converter(outputs):
    """Sets the path of an output to '' if not provided or None"""
    return [o[:2] + ('',) if len(o) < 3 or o[2] is None else o for o in outputs]


@attr.s
class BidsApp:

    app_name: str = attr.ib(
        metadata={'help_string': 
            "Name of the BIDS app. Will be used to name the 'derivatives' "
            "sub-directory where the app outputs are stored"})
    image: str = attr.ib(
        metadata={'help_string': 'Name of the BIDS app image to wrap'})
    executable: str = attr.ib(
        metadata={'help_string': 'Name of the executable within the image to run (i.e. the entrypoint of the image). Required when extending the base image and launching Arcana within it'})
    inputs: ty.List[tuple[str, type, str]] = attr.ib(
        metadata={'help_string': (
            "The inputs to be inserted into the BIDS dataset (NAME, DTYPE, BIDS_PATH)")})
    outputs: ty.List[tuple[str, type, ty.Optional[str]]] = attr.ib(
        converter=outputs_converter,
        metadata={'help_string': (
            "The outputs to be extracted from the derivatives directory (NAME, DTYPE, BIDS_PATH)")})
    parameters: ty.Dict[str, type] = attr.ib(
        metadata={'help_string': 'The parameters of the app to be exposed to the interface'},
        default=None)

    def __call__(self, name=None, frequency: Clinical or str=Clinical.session,
                 virtualisation: str=None, dataset: ty.Optional[str or Path or Dataset]=None) -> Workflow:
        """Creates a Pydra workflow which takes inputs and maps them to
        a BIDS dataset, executes a BIDS app and extracts outputs from
        the derivatives stored back in the BIDS dataset

        Parameters
        ----------
        name : str
            Name for the workflow
        frequency : Clinical
            Frequency to run the app at, i.e. per-"session" or per-"dataset"
        virtualisation : str or None
            The virtualisation method to run the main app task, can be one of
            None, 'docker' or 'singularity'
        dataset : str or Dataset
            The dataset to run the BIDS app on. If a string or Path is provided
            then a new BIDS dataset is created at that location with a single
            subject (sub-DEFAULT). If nothing is provided then a dataset is
            created at './bids_dataset'.

        Returns
        -------
        pydra.Workflow
            A Pydra workflow 
        """
        if self.parameters is None:
            parameters = {}
        if isinstance(frequency, str):
            frequency = Clinical[frequency]
        if name is None:
            name = self.app_name

        # Create BIDS dataset to hold translated data
        if dataset is None:
            dataset = Path(tempfile.mkdtemp()) / 'bids_dataset'
        if not isinstance(dataset, Dataset):
            dataset = BidsDataset.create(
                path=dataset,
                name=name + '_dataset',
                subject_ids=[self.DEFAULT_ID])

        # Ensure output paths all start with 'derivatives
        input_names = [i[0] for i in self.inputs]
        output_names = [o[0] for o in self.outputs]
        workflow = Workflow(
            name=name,
            input_spec=input_names + list(parameters) + ['id'])

        # Check id startswith 'sub-' as per BIDS
        workflow.add(bidsify_id(name='bidsify_id', id=workflow.lzin.id))

        # Can't use a decorated function as we need to allow for dynamic
        # arguments
        workflow.add(func_task(
            to_bids,
            in_fields=(
                [('frequency', Clinical),
                 ('inputs', ty.List[tuple[str, type, str]]),
                 ('dataset', Dataset or str),
                 ('id', str)]
                + [(i, str) for i in input_names]),
            out_fields=[('dataset', BidsDataset)],
            name='to_bids',
            frequency=frequency,
            inputs=self.inputs,
            dataset=dataset,
            id=workflow.bidsify_id.lzout.out,
            **{i: getattr(workflow.lzin, i) for i in input_names}))

        workflow.add(dataset_paths(
            app_name=self.app_name,
            dataset=workflow.to_bids.lzout.dataset,
            id=workflow.bidsify_id.lzout.out))
            
        app_completed = self.add_main_task(
            workflow=workflow,
            dataset_path=workflow.dataset_paths.lzout.base,
            output_path=workflow.dataset_paths.lzout.output,
            parameters={p: type(p) for p in parameters},
            frequency=frequency,
            id=workflow.bidsify_id.lzout.no_prefix,
            virtualisation=virtualisation)

        workflow.add(func_task(
            extract_bids,
            in_fields=[
                ('dataset', Dataset),
                ('frequency', Clinical),
                ('outputs', ty.List[tuple[str, type, str]]),
                ('path_prefix', str),
                ('id', str),
                ('app_completed', bool)],
            out_fields=[(o, str) for o in output_names],
            name='extract_bids',
            dataset=workflow.to_bids.lzout.dataset,
            path_prefix=workflow.dataset_paths.lzout.path_prefix,
            frequency=frequency,
            outputs=self.outputs,
            id=workflow.bidsify_id.lzout.out,
            app_completed=app_completed))

        for output_name in output_names:
            workflow.set_output(
                (output_name, getattr(workflow.extract_bids.lzout, output_name)))

        return workflow

    def add_main_task(self,
                      workflow: Workflow,
                      dataset_path: LazyField,
                      output_path: LazyField,
                      frequency: Clinical,
                      id: str,
                      parameters: ty.Dict[str, type]=None,
                      virtualisation=None) -> ShellCommandTask:

        if parameters is None:
            parameters = {}

        input_fields = [
            ("dataset_path", str,
             {"help_string": "Path to BIDS dataset in the container",
              "position": 1,
              "mandatory": True,
              "argstr": ""}),
            ("output_path", str,
             {"help_string": "Directory where outputs will be written in the container",
              "position": 2,
              "argstr": ""}),
            ("analysis_level", str,
             {"help_string": "The analysis level the app will be run at",
              "position": 3,
              "argstr": ""}),
            ("participant_label", ty.List[str],
             {"help_string": "The IDs to include in the analysis",
              "argstr": "--participant_label ",
              "position": 4})]

        output_fields=[
            ("completed", bool,
             {"help_string": "a simple flag to indicate app has completed",
              "callable": lambda: True})]

        for param, dtype in parameters.items():
            argstr = f'--{param}'
            if dtype is not bool:
                argstr += ' %s'
            input_fields.append((
                param, dtype, {
                    "help_string": f"Optional parameter {param}",
                    "argstr": argstr}))

        kwargs = {p: getattr(workflow.lzin, p) for p in parameters}

        if virtualisation is None:
            task_cls = ShellCommandTask
            base_spec_cls = ShellSpec
            kwargs['executable'] = self.executable
            app_output_path = output_path
        else:

            workflow.add(make_bindings(
                name='make_bindings',
                dataset_path=dataset_path))

            kwargs['bindings'] = workflow.make_bindings.lzout.bindings

            # Set input and output directories to "internal" paths within the
            # container
            dataset_path = self.CONTAINER_DATASET_PATH
            app_output_path = self.CONTAINER_DERIV_PATH
            kwargs['image'] = self.image

            if virtualisation == 'docker':
                task_cls = DockerTask
                base_spec_cls = DockerSpec
            elif virtualisation == 'singularity':
                task_cls = SingularityTask
                base_spec_cls = SingularitySpec
            else:
                raise ArcanaUsageError(
                    f"Unrecognised container type {virtualisation} "
                    "(can be docker or singularity)")

        if frequency == Clinical.session:
            analysis_level = 'participant'
            kwargs['participant_label'] = id
        else:
            analysis_level = 'group'

        workflow.add(task_cls(
            name='bids_app',
            input_spec=SpecInfo(name="Input", fields=input_fields,
                                bases=(base_spec_cls,)),
            output_spec=SpecInfo(name="Output", fields=output_fields,
                                 bases=(ShellOutSpec,)),
            dataset_path=dataset_path,
            output_path=app_output_path,
            analysis_level=analysis_level,
            **kwargs))

        if virtualisation is not None:
            workflow.add(copytree(
                name='copy_output_dir',
                src=workflow.make_bindings.lzout.tmp_output_dir,
                dest=output_path,
                app_completed=workflow.bids_app.lzout.completed))
            completed = workflow.copy_output_dir.lzout.out
        else:
            completed = workflow.bids_app.lzout.completed

        return completed

    # For running 
    CONTAINER_DERIV_PATH = '/arcana_bids_outputs'
    CONTAINER_DATASET_PATH = '/arcana_bids_dataset'

    DEFAULT_ID = 'sub-DEFAULT'


@mark.task
@mark.annotate(
    {'return':
        {'out': str,
         'no_prefix': str}})
def bidsify_id(id):
    if id == attr.NOTHING:
        id = BidsApp.DEFAULT_ID
    else:
        id = re.sub(r'[^a-zA-Z0-9]', '', id)
        if not id.startswith('sub-'):
            id = 'sub-' + id
    return id, id[len('sub-'):]


def to_bids(frequency, inputs, dataset, id, **input_values):
    """Takes generic inptus and stores them within a BIDS dataset
    """
    for inpt_name, inpt_type, inpt_path in inputs:
        dataset.add_sink(inpt_name, inpt_type, path=inpt_path)
    data_node = dataset.node(frequency, id)
    with dataset.store:
        for inpt_name, inpt_value in input_values.items():
            node_item = data_node[inpt_name]
            node_item.put(inpt_value) # Store value/path in store
    return dataset


@mark.task
@mark.annotate(
    {'return':
        {'base': str,
         'path_prefix': str,
         'output': str}})
def dataset_paths(app_name: str, dataset: Dataset, id: str):
    return (str(dataset.id),
            'derivatives' + '/' + app_name,
            str(dataset.id / 'derivatives' / app_name / id))


def extract_bids(dataset: Dataset,
                 frequency: Clinical,
                 outputs: ty.List[tuple[str, type, str]],
                 path_prefix: str,
                 id: str,
                 app_completed: bool):
    """Selects the items from the dataset corresponding to the input 
    sources and retrieves them from the store to a cache on 
    the host

    Parameters
    ----------
    """
    output_paths = []
    data_node = dataset.node(frequency, id)
    for output_name, output_type, output_path in outputs:
        dataset.add_sink(output_name, output_type,
                         path=path_prefix + '/' + output_path)
    with dataset.store:
        for output in outputs:
            item = data_node[output[0]]
            item.get()  # download to host if required
            output_paths.append(item.value)
    return tuple(output_paths) if len(outputs) > 1 else output_paths[0]


@mark.task
@mark.annotate(
    {'return':
        {'bindings': ty.List[tuple[str, str, str]],
         'tmp_output_dir': Path}})
def make_bindings(dataset_path: str):
    """Make bindings for directories to be mounted inside the container
        for both the input dataset and the output derivatives"""
    tmp_output_dir = tempfile.mkdtemp()
    bindings = [(str(dataset_path), BidsApp.CONTAINER_DATASET_PATH, 'ro'),
                (tmp_output_dir, BidsApp.CONTAINER_DERIV_PATH, 'rw')]
    return (bindings, Path(tmp_output_dir))


@mark.task
def copytree(src: str, dest: str, app_completed: bool) -> bool:
    shutil.copytree(src, dest)
    return app_completed