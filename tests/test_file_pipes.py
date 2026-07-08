from typing import Annotated

from fastapi import UploadFile
from fastapi.testclient import TestClient

from fanest import (
    Controller,
    FaNestFactory,
    FileTypeValidator,
    MaxFileSizeValidator,
    Module,
    ParseFilePipe,
    Post,
    UploadedFile,
    UsePipes,
)


@Controller("files")
@UsePipes(ParseFilePipe([FileTypeValidator("text/plain"), MaxFileSizeValidator(8)]))
class FileController:
    @Post("/")
    async def upload(self, file: Annotated[UploadFile, UploadedFile()]):
        return {"filename": file.filename, "type": file.content_type}


@Module(controllers=[FileController])
class FileModule:
    pass


def test_parse_file_pipe_accepts_valid_upload():
    client = TestClient(FaNestFactory.create(FileModule))

    response = client.post(
        "/files",
        files={"file": ("hello.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 200
    assert response.json() == {"filename": "hello.txt", "type": "text/plain"}


def test_parse_file_pipe_rejects_invalid_upload():
    client = TestClient(FaNestFactory.create(FileModule))

    response = client.post(
        "/files",
        files={"file": ("hello.json", b"hello", "application/json")},
    )

    assert response.status_code == 400
