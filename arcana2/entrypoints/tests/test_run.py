from argparse import ArgumentParser
from arcana2.test_fixtures.dataset import TEST_DATASET_BLUEPRINTS, make_dataset
from arcana2.test_fixtures.xnat.xnat import (
    make_mutable_dataset as make_xnat_dataset,
    TEST_DATASET_BLUEPRINTS as TEST_XNAT_DATASET_BLUEPRINTS)
from arcana2.entrypoints.run import RunCmd
from arcana2.datatypes import text


def test_run_app(work_dir):

    dataset = make_dataset(TEST_DATASET_BLUEPRINTS['basic'], work_dir)
    
    parser = ArgumentParser()
    RunCmd.construct_parser(parser)
    args = parser.parse_args([
        'arcana2.test_fixtures.tasks.concatenate',
        str(dataset.name),
        '--repository', 'file_system',
        '--input', 'in_file1', 'file1', 'text',
        '--input', 'in_file2', 'file2', 'text',
        '--output', 'out_file', 'deriv', 'text',
        '--dataspace', 'arcana2.test_fixtures.dataset.TestDataSpace',
        '--hierarchy', 'abcd',
        '--frequency', 'abcd',
        '--parameter', 'duplicates', '2'])
    RunCmd().run(args)

    dataset.add_sink('deriv', text)

    for item in dataset['deriv']:
        with open(str(item.fs_path)) as f:
            contents = f.read()
        assert contents == '\n'.join(['file1.txt', 'file2.txt'] * 2)


def test_run_xnat_app(xnat_repository, xnat_archive_dir):

    dataset = make_xnat_dataset(xnat_repository, xnat_archive_dir,
                                test_name='basic.api')
    
    parser = ArgumentParser()
    RunCmd.construct_parser(parser)
    args = parser.parse_args([
        'arcana2.test_fixtures.tasks.concatenate',
        dataset.name,
        '--input', 'in_file1', 'scan1:text',
        '--input', 'in_file2', 'scan2:text',
        '--output', 'out_file', 'deriv:text',
        '--parameter', 'duplicates', '2',
        '--work', '/work',
        '--repository', 'xnat', xnat_repository.server, xnat_repository.user, xnat_repository.password,
        '--ids', 'timepoint0group0member0',
        '--pydra_plugin', 'serial'])
    RunCmd().run(args)

    dataset.add_sink('deriv', text)

    for item in dataset['deriv']:
        item.get()
        with open(item.fs_path) as f:
            contents = f.read()
        assert contents == '\n'.join(['file1.txt', 'file2.txt'] * 2)