"""
Helper functions for generating XNAT Container Service compatible Docker
containers
"""
import json
import logging
from pathlib import Path
import site
import shutil
import tempfile
import pkg_resources
from dataclasses import dataclass
import cloudpickle as cp
from attr import NOTHING
import neurodocker as nd
from natsort import natsorted
from arcana2.dataspaces.clinical import Clinical
from arcana2.core.data.datatype import FileFormat
from arcana2.core.data import DataSpace
from arcana2.core.utils import resolve_class, DOCKER_HUB, ARCANA_PIP
from arcana2.exceptions import ArcanaUsageError
from arcana2.__about__ import PACKAGE_NAME, python_versions

logger = logging.getLogger('arcana')


def generate_dockerfile(pydra_task, json_config, maintainer, build_dir,
                        requirements=None, packages=None, extra_labels=None):
    """Constructs a dockerfile that wraps a with dependencies

    Parameters
    ----------
    pydra_task : pydra.Task or pydra.Workflow
        The Pydra Task or Workflow to be run in XNAT container service
    json_config : dict[str, Any]
        The command JSON (as generated by `generate_json_config`) to insert
        into a label of the docker file.
    maintainer : str
        The name and email of the developer creating the wrapper (i.e. you)   
    build_dir : Path
        Path to the directory to create the Dockerfile in and copy any local
        files to
    requirements : list[tuple[str, str]]
        Name and version of the Neurodocker requirements to add to the image
    packages : list[tuple[str, str]]
        Name and version of the Python PyPI packages to add to the image
    registry : str
        URI of the Docker registry to upload the image to
    extra_labels : dict[str, str], optional
        Additional labels to be added to the image

    Returns
    -------
    str
        Generated Dockerfile
    """

    labels = {}
    packages = list(packages)

    if build_dir is None:
        build_dir = tempfile.mkdtemp()
    if requirements is None:
        requirements = []
    if packages is None:
        packages = []

    if maintainer:
        labels["maintainer"] = maintainer

    pipeline_name = pydra_task.name.replace('.', '_').capitalize()

    # Convert JSON into Docker label
    labels['org.nrg.commands'] = '[' + json.dumps(json_config) + ']'
    if extra_labels:
        labels.update(extra_labels)

    instructions = [
        ["base", "debian:bullseye"],
        ["install", ["git", "vim", "ssh-client", "python3", "python3-pip"]]]

    for req in requirements:
        req_name = req[0]
        install_props = {}
        if len(req) > 1 and req[1] != '.':
            install_props['version'] = req[1]
        if len(req) > 2:
            install_props['method'] = req[2]
        instructions.append([req_name, install_props])

    arcana_pkg = next(p for p in pkg_resources.working_set
                      if p.key == PACKAGE_NAME)
    arcana_pkg_loc = Path(arcana_pkg.location).resolve()
    site_pkg_locs = [Path(p).resolve() for p in site.getsitepackages()]

    # Use local installation of arcana
    if arcana_pkg_loc not in site_pkg_locs:
        shutil.rmtree(build_dir / 'arcana')
        shutil.copytree(arcana_pkg_loc, build_dir / 'arcana')
        arcana_pip = '/arcana'
        instructions.append(['copy', ['./arcana', arcana_pip]])
    else:
        direct_url_path = Path(arcana_pkg.egg_info) / 'direct_url.json'
        if direct_url_path.exists():
            with open(direct_url_path) as f:
                durl = json.load(f)             
            arcana_pip = f"{durl['vcs']}+{durl['url']}@{durl['commit_id']}"
        else:
            arcana_pip = f"{arcana_pkg.key}=={arcana_pkg.version}"
    packages.append(arcana_pip)

    # instructions.append(['run', 'pip3 install ' + ' '.join(packages)])

    instructions.append(
        ["miniconda", {
            "create_env": "arcana",
            "conda_install": [
                "python=" + natsorted(python_versions)[-1],
                "numpy",
                "traits"],
            "pip_install": packages}])

    if labels:
        instructions.append(["label", labels])

    neurodocker_specs = {
        "pkg_manager": "apt",
        "instructions": instructions}

    dockerfile = nd.Dockerfile(neurodocker_specs).render()

    # Save generated dockerfile to file
    out_file = build_dir / 'Dockerfile'
    out_file.parent.mkdir(exist_ok=True, parents=True)
    with open(str(out_file), 'w') as f:
        f.write(dockerfile)
    logger.info("Dockerfile generated at %s", out_file)

    return dockerfile, build_dir


def generate_json_config(pipeline_name, pydra_task, image_tag,
                         inputs, outputs, description, version,
                         parameters=None, frequency=Clinical.session,
                         registry=DOCKER_HUB, info_url=None, debug_output=False):
    """Constructs the XNAT CS "command" JSON config, which specifies how XNAT
    should handle the containerised pipeline

    Parameters
    ----------
    pipeline_name : str
        Name of the pipeline
    pydra_task : Task or Workflow
        The task or workflow to be wrapped for XNAT CS use
    image_tag : str
        Name + version of the Docker image to be created
    inputs : list[InputArg]
        Inputs to be provided to the container
    outputs : list[OutputArg]
        Outputs from the container 
    description : str
        User-facing description of the pipeline
    version : str
        Version string for the wrapped pipeline
    parameters : list[str]
        Parameters to be exposed in the CS command    
    frequency : Clinical
        Frequency of the pipeline to generate (can be either 'dataset' or 'session' currently)
    registry : str
        URI of the Docker registry to upload the image to

    Returns
    -------
    dict
        JSON that can be used 

    Raises
    ------
    ArcanaUsageError
        [description]
    """
    if parameters is None:
        parameters = []
    if isinstance(frequency, str):
        frequency = Clinical[frequency]
    if frequency not in VALID_FREQUENCIES:
        raise ArcanaUsageError(
            f"'{frequency}'' is not a valid option ('"
            + "', '".join(VALID_FREQUENCIES) + "')")

    # Convert tuples to appropriate dataclasses for inputs and outputs
    inputs = [InputArg(*i) for i in inputs if isinstance(i, tuple)]
    outputs = [OutputArg(*o) for o in outputs if isinstance(o, tuple)]

    field_specs = dict(pydra_task.input_spec.fields)

    # JSON to define all inputs and parameters to the pipelines
    inputs_json = []

    # Add task inputs to inputs JSON specification
    input_args = []
    for inpt in inputs:
        spec = field_specs[inpt.name]
        
        desc = spec.metadata.get('help_string', '')
        if spec.type in (str, Path):
            desc = (f"Scan match: {desc} "
                    "[SCAN_TYPE [ORDER [TAG=VALUE, ...]]]")
            input_type = 'string'
        else:
            desc = f"Field match ({spec.type}): {desc} [FIELD_NAME]"
            input_type = COMMAND_INPUT_TYPES[spec.type]
        inputs_json.append({
            "name": inpt.name,
            "description": desc,
            "type": input_type,
            "default-value": "",
            "required": True,
            "user-settable": True,
            "replacement-key": "[{}_INPUT]".format(inpt.name.upper())})
        input_args.append(
            f'--input {inpt.name} [{inpt.name.upper()}_INPUT]')

    # Add parameters as additional inputs to inputs JSON specification
    param_args = []
    for param in parameters:
        spec = field_specs[param]
        desc = "Parameter: " + spec.metadata.get('help_string', '')
        required = spec._default is NOTHING

        inputs_json.append({
            "name": param,
            "description": desc,
            "type": COMMAND_INPUT_TYPES[spec.type],
            "default-value": (spec._default if not required else ""),
            "required": required,
            "user-settable": True,
            "replacement-key": "[{}_PARAM]".format(param.upper())})
        param_args.append(
            f'--parameter {param} [{param.upper()}_PARAM]')

    # Set up output handlers and arguments
    outputs_json = []
    output_handlers = []
    output_args = []
    for output in outputs:

        output_fname = output.name + output.datatype.extension
        # Set the path to the 
        outputs_json.append({
            "name": output.name,
            "description": f"{output.name} ({output.datatype})",
            "required": True,
            "mount": "out",
            "path": output_fname,
            "glob": None})
        output_handlers.append({
            "name": f"{output.name}-resource",
            "accepts-command-output": output.name,
            "via-wrapup-command": None,
            "as-a-child-of": "SESSION",
            "type": "Resource",
            "label": output.name,
            "format": None})
        output_args.append(
            f'--output {output.name} /output/{output_fname}')

    # Save work directory as session resource if debugging
    if debug_output:  
        outputs_json.append({
                "name": "work",
                "description": "Working directory",
                "required": True,
                "mount": "work",
                "path": None,
                "glob": None})
        output_handlers.append({
                "name": "work-resource",
                "accepts-command-output": "work",
                "via-wrapup-command": None,
                "as-a-child-of": "SESSION",
                "type": "Resource",
                "label": "__work__",
                "format": None})

    input_args_str = ' '.join(input_args)
    output_args_str = ' '.join(output_args)
    param_args_str = ' '.join(param_args)

    # Unpickle function to get its name
    func = cp.loads(pydra_task.inputs._func)

    cmdline = (
        f"conda run --no-capture-output -n arcana "  # activate conda
        f"arcana run {func.__module__}.{func.__name__} "  # run pydra task in Arcana
        f"[PROJECT_ID] {input_args_str} {output_args_str} {param_args_str} --work /work " # inputs + params
        "--repository xnat $XNAT_HOST $XNAT_USER $XNAT_PASS")  # pass XNAT API details

    # Create Project input that can be passed to the command line, which will
    # be populated by inputs derived from the XNAT object passed to the pipeline
    inputs_json.append(
        {
            "name": "PROJECT_ID",
            "description": "Project ID",
            "type": "string",
            "required": True,
            "user-settable": False,
            "replacement-key": "[PROJECT_ID]"
        })

    # Access session via Container service args and derive 
    if frequency == Clinical.session:
        # Set the object the pipeline is to be run against
        context = ["xnat:imageSessionData"]
        # Create Session input that  can be passed to the command line, which
        # will be populated by inputs derived from the XNAT session object
        # passed to the pipeline.
        inputs_json.append(
            {
                "name": "SESSION_LABEL",
                "description": "Imaging session label",
                "type": "string",
                "required": True,
                "user-settable": False,
                "replacement-key": "[SESSION_LABEL]"
            })
        # Add specific session to process to command line args
        cmdline += " --ids [SESSION_LABEL] "
        # Access the session XNAT object passed to the pipeline
        external_inputs = [
            {
                "name": "SESSION",
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
                "user-settable": False,
                "load-children": True}]
        # Access to project ID and session label from session XNAT object
        derived_inputs = [
            {
                "name": "__SESSION_LABEL__",
                "type": "string",
                "derived-from-wrapper-input": "SESSION",
                "derived-from-xnat-object-property": "label",
                "provides-value-for-command-input": "SESSION_LABEL",
                "user-settable": False
            },
            {
                "name": "__PROJECT_ID__",
                "type": "string",
                "derived-from-wrapper-input": "SESSION",
                "derived-from-xnat-object-property": "project-id",
                "provides-value-for-command-input": "PROJECT_ID",
                "user-settable": False
            }]
    
    else:
        raise NotImplementedError(
            "Wrapper currently only supports session-level pipelines")

    # Generate the complete configuration JSON
    json_config = {
        "name": pipeline_name,
        "description": description,
        "label": pipeline_name,
        "version": version,
        "schema-version": "1.0",
        "image": image_tag,
        "index": registry,
        "type": "docker",
        "command-line": cmdline,
        "override-entrypoint": True,
        "mounts": [
            {
                "name": "in",
                "writable": False,
                "path": "/input"
            },
            {
                "name": "out",
                "writable": True,
                "path": "/output"
            },
            {
                "name": "work",
                "writable": True,
                "path": "/work"
            }
        ],
        "ports": {},
        "inputs": inputs_json,
        "outputs": outputs_json,
        "xnat": [
            {
                "name": pipeline_name,
                "description": description,
                "contexts": context,
                "external-inputs": external_inputs,
                "derived-inputs": derived_inputs,
                "output-handlers": output_handlers
            }
        ]
    }

    if info_url:
        json_config['info-url'] = info_url

    return json_config


@dataclass
class InputArg():
    name: str
    datatype: FileFormat
    frequency: DataSpace

@dataclass
class OutputArg():
    name: str
    datatype: FileFormat


COMMAND_INPUT_TYPES = {
    bool: 'bool',
    str: 'string',
    int: 'number',
    float: 'number'}

VALID_FREQUENCIES = (Clinical.session, Clinical.dataset)
