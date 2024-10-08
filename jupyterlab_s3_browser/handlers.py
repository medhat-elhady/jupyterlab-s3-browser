import base64
import json
import logging
from pathlib import Path

import boto3
import s3fs
import tornado
from botocore.exceptions import NoCredentialsError, ClientError
from jupyter_server.base.handlers import APIHandler
from jupyter_server.utils import url_path_join


class DirectoryNotEmptyException(Exception):
    """Raise for attempted deletions of non-empty directories"""
    pass


def create_s3fs(config):
    if config.endpoint_url and config.client_id and config.client_secret:
        return s3fs.S3FileSystem(
            key=config.client_id,
            secret=config.client_secret,
            token=config.session_token,
            client_kwargs={"endpoint_url": config.endpoint_url},
        )
    else:
        return s3fs.S3FileSystem()


def create_s3_resource(config):
    if config.endpoint_url and config.client_id and config.client_secret:
        return boto3.resource(
            "s3",
            aws_access_key_id=config.client_id,
            aws_secret_access_key=config.client_secret,
            aws_session_token=config.session_token,
            endpoint_url=config.endpoint_url,
        )
    else:
        return boto3.resource("s3")


def _test_aws_s3_role_access():
    """
    Checks if we have access to AWS S3 through role-based access and lists only accessible buckets.
    """
    s3 = boto3.client("s3")
    all_buckets = s3.list_buckets()["Buckets"]
    accessible_buckets = []

    for bucket in all_buckets:
        try:
            # Try to list objects in the bucket to verify access
            s3.list_objects_v2(Bucket=bucket["Name"], MaxKeys=1)
            accessible_buckets.append(
                {"name": bucket["Name"] + "/", "path": bucket["Name"] + "/", "type": "directory"}
            )
        except ClientError as e:
            # Ignore access denied errors
            if e.response["Error"]["Code"] == "AccessDenied":
                logging.info(f"Access denied for bucket: {bucket['Name']}")
            else:
                raise e

    return accessible_buckets


def has_aws_s3_role_access():
    """
    Returns true if the user has access to an aws S3 bucket
    """
    aws_credentials_file = Path("{}/.aws/credentials".format(Path.home()))
    if aws_credentials_file.exists():
        with aws_credentials_file.open() as credentials_file:
            for line in credentials_file.readlines():
                if line.startswith("aws_access_key_id"):
                    access_key_id = line.split("=", 1)[1].strip()
                    if not access_key_id.startswith("AKIA") and not access_key_id.startswith("ASIA"):
                        logging.info(
                            "Found invalid AWS aws_access_key_id in ~/.aws/credentials file, "
                            "will not attempt to authenticate through ~/.aws/credentials."
                        )
                        return False

    try:
        _test_aws_s3_role_access()
        return True
    except NoCredentialsError:
        return False
    except Exception as e:
        logging.error(e)
        return False

def check_bucket_access(bucket_name):
    # Initialize a session using Amazon S3
    s3 = boto3.client('s3')
    
    try:
        # Try to access the bucket by listing its contents
        s3.head_bucket(Bucket=bucket_name)
        return True
    except:
        return False
    

def test_s3_credentials(endpoint_url, client_id, client_secret, session_token):
    """
    Checks if we're able to list buckets with these credentials.
    If not, it throws an exception.
    """
    test = boto3.resource(
        "s3",
        aws_access_key_id=client_id,
        aws_secret_access_key=client_secret,
        endpoint_url=endpoint_url,
        aws_session_token=session_token,
    )
    all_buckets = test.buckets.all()
    logging.debug(
        [
            {"name": bucket.name + "/", "path": bucket.name + "/", "type": "directory"}
            for bucket in all_buckets if check_bucket_access(bucket.name)
        ]
    )


class AuthHandler(APIHandler):  # pylint: disable=abstract-method
    @property
    def config(self):
        return self.settings["s3_config"]

    @tornado.web.authenticated
    def get(self, path=""):
        """
        Checks if the user is already authenticated against an s3 instance.
        """
        authenticated = False
        if has_aws_s3_role_access():
            authenticated = True

        if not authenticated:
            try:
                config = self.config
                if config.endpoint_url and config.client_id and config.client_secret:
                    test_s3_credentials(
                        config.endpoint_url,
                        config.client_id,
                        config.client_secret,
                        config.session_token,
                    )
                    logging.debug("...successfully authenticated")
                    authenticated = True

            except Exception as err:
                logging.debug("...failed to authenticate")
                logging.debug(err)

        self.finish(json.dumps({"authenticated": authenticated}))


class S3ResourceNotFoundException(Exception):
    pass


def convertS3FStoJupyterFormat(result):
    return {
        "name": result["Key"].rsplit("/", 1)[-1],
        "path": result["Key"],
        "type": result["type"],
    }


class S3Handler(APIHandler):
    @property
    def config(self):
        return self.settings["s3_config"]

    s3fs = None
    s3_resource = None

    @tornado.web.authenticated
    def get(self, path=""):
        """
        Takes a path and returns lists of files/objects
        and directories/prefixes based on the path.
        """
        path = path

        try:
            if not self.s3fs:
                self.s3fs = create_s3fs(self.config)

            self.s3fs.invalidate_cache()

            if (path and not path.endswith("/")) and ("X-Custom-S3-Is-Dir" not in self.request.headers):
                with self.s3fs.open(path, "rb") as f:
                    result = {
                        "path": path,
                        "type": "file",
                        "content": base64.encodebytes(f.read()).decode("ascii"),
                    }
            else:
                raw_result = list(map(convertS3FStoJupyterFormat, self.s3fs.listdir(path)))
                result = list(filter(lambda x: x["name"] != "", raw_result))

        except S3ResourceNotFoundException as e:
            result = {
                "error": 404,
                "message": "The requested resource could not be found.",
            }
        except Exception as e:
            logging.error(f"Exception encountered during GET {path}: {e}")
            result = {"error": 500, "message": str(e)}

        self.finish(json.dumps(result))

    @tornado.web.authenticated
    def put(self, path=""):
        """
        Takes a path and returns lists of files/objects
        and directories/prefixes based on the path.
        """
        path = path

        result = {}

        try:
            if not self.s3fs:
                self.s3fs = create_s3fs(self.config)

            if "X-Custom-S3-Copy-Src" in self.request.headers:
                source = self.request.headers["X-Custom-S3-Copy-Src"]
                if "/" not in source:
                    path = path + "/.keep"
                self.s3fs.cp(source, path, recursive=True)
                with self.s3fs.open(path, "rb") as f:
                    result = {
                        "path": path,
                        "type": "file",
                        "content": base64.encodebytes(f.read()).decode("ascii"),
                    }
            elif "X-Custom-S3-Move-Src" in self.request.headers:
                source = self.request.headers["X-Custom-S3-Move-Src"]
                self.s3fs.move(source, path, recursive=True)
                with self.s3fs.open(path, "rb") as f:
                    result = {
                        "path": path,
                        "type": "file",
                        "content": base64.encodebytes(f.read()).decode("ascii"),
                    }
            elif "X-Custom-S3-Is-Dir" in self.request.headers:
                path = path.lower()
                if not path.endswith("/"):
                    path = path + "/"
                self.s3fs.mkdir(path)
                self.s3fs.touch(path + ".keep")
            elif self.request.body:
                request = json.loads(self.request.body)
                with self.s3fs.open(path, "w") as f:
                    f.write(request["content"])
                    result = {
                        "path": path,
                        "type": "file",
                        "content": request["content"],
                    }

        except S3ResourceNotFoundException as e:
            result = {
                "error": 404,
                "message": "The requested resource could not be found.",
            }
        except DirectoryNotEmptyException as e:
            result = {"error": 400, "message": "Directory not empty"}
        except Exception as e:
            logging.error("error while deleting")
            logging.error(e)
            result = {"error": 500, "message": str(e)}

        self.finish(json.dumps(result))

    @tornado.web.authenticated
    def delete(self, path=""):
        """
        Takes a path and returns lists of files/objects
        and directories/prefixes based on the path.
        """
        path = path
        #  logging.info("DELETE: {}".format(path))

        result = {}

        try:
            if not self.s3fs:
                self.s3fs = create_s3fs(self.config)
            if not self.s3_resource:
                self.s3_resource = create_s3_resource(self.config)

            if self.s3fs.exists(path + "/.keep"):
                self.s3fs.rm(path + "/.keep")

            objects_matching_prefix = self.s3fs.listdir(path + "/")
            is_directory = (len(objects_matching_prefix) > 1) or (
                (len(objects_matching_prefix) == 1)
                and objects_matching_prefix[0]["Key"] != path
            )

            if is_directory:
                if (len(objects_matching_prefix) > 1) or (
                    (len(objects_matching_prefix) == 1)
                    and objects_matching_prefix[0]["Key"] != path + "/"
                ):
                    raise DirectoryNotEmptyException()
                else:
                    # for some reason s3fs.rm doesn't work reliably
                    if path.count("/") > 1:
                        bucket_name, prefix = path.split("/", 1)
                        bucket = self.s3_resource.Bucket(bucket_name)
                        bucket.objects.filter(Prefix=prefix).delete()
                    else:
                        self.s3fs.rm(path)
            else:
                self.s3fs.rm(path)

        except S3ResourceNotFoundException as e:
            logging.error(e)
            result = {
                "error": 404,
                "message": "The requested resource could not be found.",
            }
        except DirectoryNotEmptyException as e:
            #  logging.info("Attempted to delete non-empty directory")
            result = {"error": 400, "error": "DIR_NOT_EMPTY"}
        except Exception as e:
            logging.error("error while deleting")
            logging.error(e)
            result = {"error": 500, "message": str(e)}

        self.finish(json.dumps(result))


def setup_handlers(web_app):
    host_pattern = ".*"

    base_url = web_app.settings["base_url"]
    handlers = [
        (url_path_join(base_url, "jupyterlab_s3_browser", "auth(.*)"), AuthHandler),
        (url_path_join(base_url, "jupyterlab_s3_browser", "files(.*)"), S3Handler),
    ]
    web_app.add_handlers(host_pattern, handlers)
