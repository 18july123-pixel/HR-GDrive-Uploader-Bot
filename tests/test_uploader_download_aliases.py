import importlib


def test_uploader_download_and_filename_aliases_exist():
    mod = importlib.import_module("url_uploader")
    assert hasattr(mod, "start_download_job")
    assert hasattr(mod, "sanitize_file_stem")
    assert mod.start_download_job is mod.start_download
    assert mod.sanitize_file_stem is mod.sanitize_filename


def test_uploader_progress_hook_factory_exists_for_thread_safe_loop_context():
    mod = importlib.import_module("url_uploader")
    assert hasattr(mod, "make_progress_hook")
    loop = object()
    hook = mod.make_progress_hook(loop, None)
    assert callable(hook)
