from typing import Annotated
from io import BytesIO
from types import SimpleNamespace

from fastapi import UploadFile
from fastapi.testclient import TestClient

from fanest import (
    Controller,
    FaNestFactory,
    AnyFilesInterceptor,
    FileFieldsInterceptor,
    FileTypeValidator,
    FileInterceptor,
    FilesInterceptor,
    MaxFileSizeValidator,
    Module,
    ParseFilePipe,
    ParseFilePipeBuilder,
    Post,
    UploadedFile,
    UploadedFiles,
    UploadField,
    UseInterceptors,
    UsePipes,
    disk_storage,
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


def test_parse_file_pipe_builder_and_size_validator_measure_file_objects_without_size():
    raw_file = BytesIO(b"hello")
    upload = SimpleNamespace(filename="hello.txt", content_type="text/plain", file=raw_file)
    pipe = (
        ParseFilePipeBuilder()
        .add_file_type_validator({"fileType": "text/plain"})
        .add_max_size_validator({"maxSize": 8})
        .build()
    )

    assert pipe.transform(upload, {"name": "file"}) is upload
    assert raw_file.tell() == 0


def test_file_interceptor_enforces_limits_filter_and_disk_storage(tmp_path):
    upload_dir = tmp_path / "uploads"

    @Controller("intercept-files")
    class InterceptFileController:
        @UseInterceptors(
            FileInterceptor(
                "avatar",
                limits={"fileSize": 5},
                file_filter=lambda file: file.content_type == "text/plain",
                storage=disk_storage(
                    destination=upload_dir,
                    filename=lambda file: f"stored-{file.filename}",
                ),
            )
        )
        @Post("/avatar")
        async def avatar(self, file=UploadedFile("avatar")):
            return {
                "filename": file.filename,
                "stored_path": file.stored_path,
                "stored": (upload_dir / f"stored-{file.filename}").read_text(),
            }

    @Module(controllers=[InterceptFileController])
    class InterceptFileModule:
        pass

    client = TestClient(FaNestFactory.create(InterceptFileModule))

    accepted = client.post(
        "/intercept-files/avatar",
        files={"avatar": ("hello.txt", b"hello", "text/plain")},
    )
    too_large = client.post(
        "/intercept-files/avatar",
        files={"avatar": ("large.txt", b"too-big", "text/plain")},
    )
    rejected_type = client.post(
        "/intercept-files/avatar",
        files={"avatar": ("hello.json", b"{}", "application/json")},
    )

    assert accepted.status_code == 200
    assert accepted.json()["stored"] == "hello"
    assert accepted.json()["stored_path"].endswith("stored-hello.txt")
    assert too_large.status_code == 413
    assert rejected_type.status_code == 400


def test_file_interceptor_rejects_invalid_limit_options():
    try:
        FileInterceptor("avatar", limits={"fileSize": -1})
    except ValueError as exc:
        assert "non-negative" in str(exc)
    else:
        raise AssertionError("negative fileSize limit should fail")

    try:
        FilesInterceptor("photos", max_count=-1)
    except ValueError as exc:
        assert "non-negative" in str(exc)
    else:
        raise AssertionError("negative max_count should fail")

    try:
        FileFieldsInterceptor([{"name": "photos", "maxCount": "many"}])
    except ValueError as exc:
        assert "integer" in str(exc)
    else:
        raise AssertionError("non-integer maxCount should fail")


def test_disk_storage_sanitizes_filenames_and_rejects_file_destinations(tmp_path):
    upload_dir = tmp_path / "uploads"
    file_destination = tmp_path / "not-a-dir"
    file_destination.write_text("occupied", encoding="utf-8")

    @Controller("storage-edges")
    class StorageEdgeController:
        @UseInterceptors(
            FileInterceptor(
                "avatar",
                storage=disk_storage(destination=upload_dir, filename=lambda file: "../safe.txt"),
            )
        )
        @Post("/safe-name")
        async def safe_name(self, file=UploadedFile("avatar")):
            return {
                "stored_path": file.stored_path,
                "files": sorted(path.name for path in upload_dir.iterdir()),
            }

        @UseInterceptors(
            FileInterceptor("avatar", storage=disk_storage(destination=file_destination))
        )
        @Post("/bad-destination")
        async def bad_destination(self, file=UploadedFile("avatar")):
            return {"filename": file.filename}

    @Module(controllers=[StorageEdgeController])
    class StorageEdgeModule:
        pass

    client = TestClient(FaNestFactory.create(StorageEdgeModule))

    accepted = client.post(
        "/storage-edges/safe-name",
        files={"avatar": ("avatar.txt", b"ok", "text/plain")},
    )
    rejected = client.post(
        "/storage-edges/bad-destination",
        files={"avatar": ("avatar.txt", b"ok", "text/plain")},
    )

    assert accepted.status_code == 200
    assert accepted.json()["files"] == ["safe.txt"]
    assert accepted.json()["stored_path"].endswith("/safe.txt")
    assert not (tmp_path / "safe.txt").exists()
    assert rejected.status_code == 400


def test_files_interceptor_enforces_max_count_and_optional_multi_uploads():
    @Controller("multi-files")
    class MultiFileController:
        @UseInterceptors(FilesInterceptor("photos", max_count=2))
        @Post("/photos")
        async def photos(self, files=UploadedFiles("photos")):
            return {"filenames": [file.filename for file in files]}

        @Post("/optional")
        async def optional(self, files=UploadedFiles("photos", default=None)):
            return {"count": len(files or [])}

    @Module(controllers=[MultiFileController])
    class MultiFileModule:
        pass

    client = TestClient(FaNestFactory.create(MultiFileModule))

    accepted = client.post(
        "/multi-files/photos",
        files=[
            ("photos", ("one.txt", b"1", "text/plain")),
            ("photos", ("two.txt", b"2", "text/plain")),
        ],
    )
    too_many = client.post(
        "/multi-files/photos",
        files=[
            ("photos", ("one.txt", b"1", "text/plain")),
            ("photos", ("two.txt", b"2", "text/plain")),
            ("photos", ("three.txt", b"3", "text/plain")),
        ],
    )
    optional = client.post("/multi-files/optional")

    assert accepted.status_code == 200
    assert accepted.json() == {"filenames": ["one.txt", "two.txt"]}
    assert too_many.status_code == 400
    assert optional.status_code == 200
    assert optional.json() == {"count": 0}


def test_file_fields_interceptor_enforces_per_field_counts_and_filter():
    @Controller("field-files")
    class FieldFileController:
        @UseInterceptors(
            FileFieldsInterceptor(
                [
                    UploadField("avatar", max_count=1),
                    {"name": "gallery", "maxCount": 2},
                ],
                file_filter=lambda file: file.content_type == "text/plain",
            )
        )
        @Post("/")
        async def upload(
            self,
            avatar=UploadedFile("avatar"),
            gallery=UploadedFiles("gallery", default=None),
        ):
            return {
                "avatar": avatar.filename,
                "gallery": [file.filename for file in gallery or []],
            }

    @Module(controllers=[FieldFileController])
    class FieldFileModule:
        pass

    client = TestClient(FaNestFactory.create(FieldFileModule))

    accepted = client.post(
        "/field-files",
        files=[
            ("avatar", ("avatar.txt", b"1", "text/plain")),
            ("gallery", ("one.txt", b"2", "text/plain")),
            ("gallery", ("two.txt", b"3", "text/plain")),
        ],
    )
    too_many = client.post(
        "/field-files",
        files=[
            ("avatar", ("avatar.txt", b"1", "text/plain")),
            ("gallery", ("one.txt", b"2", "text/plain")),
            ("gallery", ("two.txt", b"3", "text/plain")),
            ("gallery", ("three.txt", b"4", "text/plain")),
        ],
    )
    rejected = client.post(
        "/field-files",
        files=[
            ("avatar", ("avatar.json", b"{}", "application/json")),
        ],
    )

    assert accepted.status_code == 200
    assert accepted.json() == {"avatar": "avatar.txt", "gallery": ["one.txt", "two.txt"]}
    assert too_many.status_code == 400
    assert rejected.status_code == 400


def test_any_files_interceptor_applies_limits_across_declared_file_parameters():
    @Controller("any-files")
    class AnyFileController:
        @UseInterceptors(AnyFilesInterceptor(max_count=2, limits={"fileSize": 4}))
        @Post("/")
        async def upload(
            self,
            avatar=UploadedFile("avatar"),
            gallery=UploadedFiles("gallery", default=None),
        ):
            return {"count": 1 + len(gallery or [])}

    @Module(controllers=[AnyFileController])
    class AnyFileModule:
        pass

    client = TestClient(FaNestFactory.create(AnyFileModule))

    accepted = client.post(
        "/any-files",
        files=[
            ("avatar", ("avatar.txt", b"1", "text/plain")),
            ("gallery", ("one.txt", b"2", "text/plain")),
        ],
    )
    too_many = client.post(
        "/any-files",
        files=[
            ("avatar", ("avatar.txt", b"1", "text/plain")),
            ("gallery", ("one.txt", b"2", "text/plain")),
            ("gallery", ("two.txt", b"3", "text/plain")),
        ],
    )
    too_large = client.post(
        "/any-files",
        files=[
            ("avatar", ("avatar.txt", b"large", "text/plain")),
        ],
    )

    assert accepted.status_code == 200
    assert accepted.json() == {"count": 2}
    assert too_many.status_code == 400
    assert too_large.status_code == 413


def test_upload_interceptors_enforce_files_and_fields_limits():
    @Controller("upload-limits")
    class UploadLimitController:
        @UseInterceptors(FilesInterceptor("photos", limits={"files": 1}))
        @Post("/files")
        async def files(self, photos=UploadedFiles("photos")):
            return {"count": len(photos)}

        @UseInterceptors(
            FileFieldsInterceptor(
                [{"name": "avatar"}, {"name": "gallery"}],
                limits={"fields": 1},
            )
        )
        @Post("/fields")
        async def fields(
            self,
            avatar=UploadedFile("avatar"),
            gallery=UploadedFiles("gallery", default=None),
        ):
            return {"avatar": avatar.filename, "count": len(gallery or [])}

    @Module(controllers=[UploadLimitController])
    class UploadLimitModule:
        pass

    client = TestClient(FaNestFactory.create(UploadLimitModule))

    too_many_files = client.post(
        "/upload-limits/files",
        files=[
            ("photos", ("one.txt", b"1", "text/plain")),
            ("photos", ("two.txt", b"2", "text/plain")),
        ],
    )
    too_many_fields = client.post(
        "/upload-limits/fields",
        files=[
            ("avatar", ("avatar.txt", b"1", "text/plain")),
            ("gallery", ("one.txt", b"2", "text/plain")),
        ],
    )

    assert too_many_files.status_code == 400
    assert too_many_fields.status_code == 400
