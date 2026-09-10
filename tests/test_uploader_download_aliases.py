import importlib


def test_uploader_download_and_filename_aliases_exist():
    mod = importlib.import_module("url_uploader")
    assert hasattr(mod, "start_download_job")
    assert hasattr(mod, "sanitize_file_stem")
    assert mod.start_download_job is mod.start_download
    assert mod.sanitize_file_stem is mod.sanitize_filename
