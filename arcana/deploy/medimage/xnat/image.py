import sys
from pathlib import Path
import tempfile
import json
from copy import copy
import attrs
from neurodocker.reproenv import DockerRenderer
from arcana.data.stores.medimage import XnatViaCS
from arcana.core.utils import class_location
from arcana.core.data.store import DataStore
from arcana.core.deploy.image import ContainerImageSpec
from arcana.core.deploy.utils import DOCKER_HUB


@attrs.define
class XnatContainerImageSpec(ContainerImageSpec):
    def construct_dockerfile(
        self,
        build_dir: Path,
        docker_registry: str = DOCKER_HUB,
        test_config: bool = False,
        **kwargs,
    ):
        """Creates a Docker image containing one or more XNAT commands ready
        to be installed in XNAT's container service plugin

        Parameters
        ----------
        build_dir : Path
            the directory to build the docker image within, i.e. where to write
            Dockerfile and supporting files to be copied within the image
        image_tag: str
            the tag of the image that is being built
        test_config : bool
            whether to create the container so that it will work with the test
            XNAT configuration (i.e. hard-coding the XNAT server IP)
        **kwargs:
            Passed on to super `construct_dockerfile` method

        Returns
        -------
        DockerRenderer
            the Neurodocker renderer
        Path
            path to build directory
        """

        dockerfile = super().construct_dockerfile(build_dir, **kwargs)

        xnat_commands = []
        for cmd_spec in self.commands:

            if "info_url" not in cmd_spec:
                cmd_spec = copy(cmd_spec)
                cmd_spec["info_url"] = self.info_url

            xnat_cmd = cmd_spec.make_json(
                image_tag=self.tag,
                registry=self.registry,
                pkg_version=self.pkg_version,
                wrapper_version=self.wrapper_version,
                **cmd_spec,
            )

            xnat_commands.append(xnat_cmd)

        # Copy the generated XNAT commands inside the container for ease of reference
        self.copy_command_ref(dockerfile, xnat_commands, build_dir)

        self.save_store_config(dockerfile, build_dir, test_config=test_config)

        # Convert XNAT command label into string that can by placed inside the
        # Docker label
        command_label = json.dumps(xnat_commands).replace("$", r"\$")

        self.add_labels(
            {"org.nrg.commands": command_label, "maintainer": self.authors[0]["email"]}
        )

        return dockerfile

    def copy_command_ref(self, dockerfile: DockerRenderer, xnat_commands, build_dir):
        """Generate Neurodocker instructions to copy a version of the XNAT commands
        into the image for reference

        Parameters
        ----------
        dockerfile : DockerRenderer
            Neurodocker renderer to build
        xnat_commands : list[dict]
            XNAT command JSONs to copy into the Dockerfile for reference
        build_dir : Path
            path to build directory
        """
        # Copy command JSON inside dockerfile for ease of reference
        cmds_dir = build_dir / "xnat_commands"
        cmds_dir.mkdir(exist_ok=True)
        for cmd in xnat_commands:
            fname = cmd.get("name", "command") + ".json"
            with open(cmds_dir / fname, "w") as f:
                json.dump(cmd, f, indent="    ")
        dockerfile.copy(source=["./xnat_commands"], destination="/xnat_commands")

    def save_store_config(
        self, dockerfile: DockerRenderer, build_dir: Path, test_config=False
    ):
        """Save a configuration for a XnatViaCS store.

        Parameters
        ----------
        dockerfile : DockerRenderer
            Neurodocker renderer to build
        build_dir : Path
            the build directory to save supporting files
        test_config : bool
            whether the target XNAT is using the local test configuration, in which
            case the server location will be hard-coded rather than rely on the
            XNAT_HOST environment variable passed to the container by the XNAT CS
        """
        xnat_cs_store_entry = {"class": "<" + class_location(XnatViaCS) + ">"}
        if test_config:
            if sys.platform == "linux":
                ip_address = "172.17.0.1"  # Linux + GH Actions
            else:
                ip_address = "host.docker.internal"  # Mac/Windows local debug
            xnat_cs_store_entry["server"] = "http://" + ip_address + ":8080"
        DataStore.save_entries(
            {"xnat-cs": xnat_cs_store_entry}, config_path=build_dir / "stores.yaml"
        )
        dockerfile.run(command="mkdir -p /root/.arcana")
        dockerfile.run(command=f"mkdir -p {str(XnatViaCS.CACHE_DIR)}")
        dockerfile.copy(
            source=["./stores.yaml"],
            destination=self.IN_DOCKER_ARCANA_HOME_DIR + "/stores.yaml",
        )
        dockerfile.env(ARCANA_HOME=self.IN_DOCKER_ARCANA_HOME_DIR)

    def create_metapackage(
        self, image_tag, manifest, build_dir: Path = None, use_local_packages=False
    ):

        if build_dir is None:
            build_dir = Path(tempfile.mkdtemp())

        dockerfile = self.init_dockerfile()

        self.install_arcana(
            dockerfile, build_dir=build_dir, use_local_package=use_local_packages
        )

        with open(build_dir / "manifest.json", "w") as f:
            json.dump(manifest, f)

        dockerfile.copy(["./manifest.json"], "/manifest.json")

        dockerfile.entrypoint(
            [
                "conda",
                "run",
                "--no-capture-output",
                "-n",
                "arcana",
                "arcana",
                "deploy",
                "xnat",
                "pull-images",
                "/manifest.json",
            ]
        )

        self.build(dockerfile, build_dir, image_tag)
