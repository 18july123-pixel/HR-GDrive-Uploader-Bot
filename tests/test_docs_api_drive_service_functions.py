import inspect

import drive_service


def test_drive_service_exposes_docs_and_sheets_rest_helpers():
    assert hasattr(drive_service, "get_docs")
    assert hasattr(drive_service, "get_document")
    assert hasattr(drive_service, "create_document")
    assert hasattr(drive_service, "append_document_text")
    assert hasattr(drive_service, "batch_update_document")
    assert hasattr(drive_service, "update_spreadsheet_values")
    assert hasattr(drive_service, "clear_spreadsheet_values")


def test_drive_service_docs_and_sheets_helpers_have_expected_http_shape():
    assert inspect.signature(drive_service.get_docs).parameters
    assert inspect.signature(drive_service.get_document).parameters
    assert inspect.signature(drive_service.create_document).parameters
    assert inspect.signature(drive_service.append_document_text).parameters
    assert inspect.signature(drive_service.batch_update_document).parameters
    assert inspect.signature(drive_service.update_spreadsheet_values).parameters
    assert inspect.signature(drive_service.clear_spreadsheet_values).parameters
