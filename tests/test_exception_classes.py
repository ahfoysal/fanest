from fanest import (
    BadRequestException,
    BaseExceptionFilter,
    GatewayTimeoutException,
    MethodNotAllowedException,
    NotImplementedException,
    RequestTimeoutException,
    ServiceUnavailableException,
    UnsupportedMediaTypeException,
)


def test_http_exception_classes_keep_status_codes():
    assert BadRequestException().status_code == 400
    assert MethodNotAllowedException().status_code == 405
    assert NotImplementedException().status_code == 501
    assert RequestTimeoutException().status_code == 408
    assert UnsupportedMediaTypeException().status_code == 415
    assert GatewayTimeoutException().status_code == 504
    assert ServiceUnavailableException().status_code == 503


def test_catch_can_be_used_with_fastapi_base_filter():
    assert isinstance(NotImplementedException(), Exception)
    assert isinstance(BaseExceptionFilter(), object)
