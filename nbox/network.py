"""
Network functions are gateway between NBX-Services. If you find yourself using this
you might want to reach out to us <research-at-nimblebox-dot-ai>!

But for the curious mind, many of our services work on gRPC and Protobufs. This network.py
manages the quirkyness of our backend and packs multiple steps as one function.
"""

import os
import re
import grpc
import jinja2
import fnmatch
import zipfile
import requests
from tempfile import gettempdir
from datetime import datetime, timezone
from google.protobuf.field_mask_pb2 import FieldMask

import nbox.utils as U
from nbox.auth import secret
from nbox.utils import logger, SimplerTimes
from nbox.version import __version__
from nbox.hyperloop.dag_pb2 import DAG
from nbox.init import nbox_ws_v1, nbox_grpc_stub, nbox_model_service_stub
from nbox.hyperloop.job_pb2 import  Job as JobProto
from nbox.hyperloop.common_pb2 import NBXAuthInfo, Resource, Code
from nbox.hyperloop.serve_pb2 import ModelRequest, Model
from nbox.messages import rpc, write_binary_to_file
from nbox.jobs import Schedule, Serve, Job
from nbox.hyperloop.nbox_ws_pb2 import JobRequest, UpdateJobRequest
from nbox.nbxlib.operator_spec import OperatorType as OT


#######################################################################################################################
"""
# Serving

Function related to serving of any model.
"""
#######################################################################################################################


def deploy_serving(
  init_folder: str,
  serving_name: str,
  serving_id: str = None,
  workspace_id: str = None,
  resource: Resource = None,
  wait_for_deployment: bool = False,
  exe_jinja_kwargs = {},
  *,
  _unittest = False
):
  """Use the NBX-Deploy Infrastructure"""
  # check if this is a valid folder or not
  if not os.path.exists(init_folder) or not os.path.isdir(init_folder):
    raise ValueError(f"Incorrect project at path: '{init_folder}'! nbox jobs new <name>")

  if resource is not None:
    logger.warning("Resource is coming in the following release!")
  if wait_for_deployment:
    logger.warning("Wait for deployment is coming in the following release!")

  # [TODO] - keeping this for now, if file can be zip without serving id then we can remove this
  logger.info(f"Serving name: {serving_name}")
  logger.info(f"Serving ID: {serving_id}")
  # if serving_id is None:
  #   logger.error("Could not find service ID, creating a new one")
  #   data = nbox_ws_v1.workspace.u(workspace_id).deployments(_method = "post", deployment_name = serving_name, deployment_description = "")
  #   serving_id = data["deployment_id"]
  #   logger.info(f"Serving ID: {serving_id}")
  model_name = U.get_random_name().replace("-", "_")
  logger.info(f"Model name: {model_name}")

  # zip init folder
  zip_path = zip_to_nbox_folder(
    init_folder = init_folder,
    id = serving_id,
    workspace_id = workspace_id,
    type = OT.SERVING,

    # jinja kwargs
    model_name = model_name,
    **exe_jinja_kwargs,
  )
  return _upload_serving_zip(zip_path, workspace_id, serving_id, model_name)


def _upload_serving_zip(zip_path, workspace_id, serving_id, model_name):
  file_size = os.stat(zip_path).st_size # serving in bytes

  # get bucket URL and upload the data
  response: Model = rpc(
    nbox_model_service_stub.UploadModel,
    ModelRequest(model=
      Model(
        serving_group_id=serving_id, name=model_name,
        code=Code(type=Code.Type.ZIP, size=int(max(file_size/(1024*1024), 1)),), # MBs
        type=Model.ServingType.SERVING_TYPE_NBOX_OP
        ),
      auth_info=NBXAuthInfo(workspace_id=workspace_id)
      ),
    "Could not get upload URL",
    raise_on_error=True
  )
  model_id = response.id
  deployment_id = response.serving_group_id
  logger.debug(f"model_id: {model_id}")
  logger.debug(f"deployment_id: {deployment_id}")

  # upload the file to a S3 -> don't raise for status here
  # TODO: @yashbonde use poster to upload files, requests doesn't support multipart uploads
  # https://stackoverflow.com/questions/15973204/using-python-requests-to-bridge-a-file-without-loading-into-memory
  s3_url = response.code.s3_url
  s3_meta = response.code.s3_meta
  logger.info(f"Uploading model to S3 ... (fs: {response.code.size/1024/1024:0.3f} MB)")
  r = requests.post(url=s3_url, data=s3_meta, files={"file": (s3_meta["key"], open(zip_path, "rb"))})
  try:
    r.raise_for_status()
  except:
    logger.error(f"Failed to upload model: {r.content.decode('utf-8')}")
    return

  # model is uploaded successfully, now we need to deploy it
  logger.info(f"Model uploaded successfully, deploying ...")
  response: Model = rpc(
    nbox_model_service_stub.Deploy,
    ModelRequest(model=Model(id=model_id,serving_group_id=deployment_id), auth_info=NBXAuthInfo(workspace_id=workspace_id)),
    "Could not deploy model",
    raise_on_error=True
  )

  # write out all the commands for this deployment
  logger.info("API will soon be hosted, here's how you can use it:")
  # _api = f"Operator.from_serving('{serving_id}', $NBX_TOKEN, '{workspace_id}')"
  # _cli = f"python3 -m nbox serve forward --id_or_name '{serving_id}' --workspace_id '{workspace_id}'"
  # _curl = f"curl https://api.nimblebox.ai/{serving_id}/forward"
  # logger.info(f" [python] - {_api}")
  # logger.info(f"    [CLI] - {_cli} --token $NBX_TOKEN --args")
  # logger.info(f"   [curl] - {_curl} -H 'NBX-KEY: $NBX_TOKEN' -H 'Content-Type: application/json' -d " + "'{}'")
  _webpage = f"{secret.get('nbx_url')}/workspace/{workspace_id}/deploy/{serving_id}"
  logger.info(f"  [page] - {_webpage}")

  return Serve(serving_id = serving_id, model_id = model_id, workspace_id = workspace_id)


#######################################################################################################################
"""
# Jobs

Function related to batch processing of any model.
"""
#######################################################################################################################


def deploy_job(
  init_folder: str,
  job_name: str,
  dag: DAG,
  schedule: Schedule,
  resource: Resource,
  workspace_id: str = None,
  job_id: str = None,
  exe_jinja_kwargs = {},
  *,
  _unittest = False
) -> None:
  """Upload code for a NBX-Job.

  Args:
    init_folder (str, optional): Name the folder to zip
    job_id_or_name (Union[str, int], optional): Name or ID of the job
    dag (DAG): DAG to upload
    workspace_id (str): Workspace ID to deploy to, if not specified, will use the personal workspace
    schedule (Schedule, optional): If `None` will run only once, else will schedule the job
    cache_dir (str, optional): Folder where to put the zipped file, if `None` will be `tempdir`
  Returns:
    Job: Job object
  """
  # check if this is a valid folder or not
  if not os.path.exists(init_folder) or not os.path.isdir(init_folder):
    raise ValueError(f"Incorrect project at path: '{init_folder}'! nbox jobs new <name>")
  if (job_name is None or job_name == "") and job_id == "":
    raise ValueError("Please specify a job name or ID")

  # logger.debug(f"deploy_job:\n  init_folder: {init_folder}\n  name: {job_name}\n  id: {job_id}")

  # job_id, job_name = _get_job_data(name = job_name, id = job_id, workspace_id = workspace_id)
  logger.info(f"Job name: {job_name}")
  logger.info(f"Job ID: {job_id}")

  # intialise the console logger
  URL = secret.get("nbx_url")
  logger.debug(f"Schedule: {schedule}")
  logger.debug("-" * 30 + " NBX Jobs " + "-" * 30)
  logger.debug(f"Deploying on URL: {URL}")

  # create the proto for this Operator
  job_proto = JobProto(
    id = job_id,
    name = job_name or U.get_random_name(True).split("-")[0],
    created_at = SimplerTimes.get_now_pb(),
    schedule = schedule.get_message() if schedule is not None else None,
    dag = dag,
    resource = resource
  )
  write_binary_to_file(job_proto, U.join(init_folder, "job_proto.msg"))

  if _unittest:
    return job_proto

  # zip the entire init folder to zip
  zip_path = zip_to_nbox_folder(
    init_folder = init_folder,
    id = job_id,
    workspace_id = workspace_id,
    type = OT.JOB,
    **exe_jinja_kwargs,
  )
  return _upload_job_zip(zip_path, job_proto,workspace_id)

def _upload_job_zip(zip_path: str, job_proto: JobProto,workspace_id: str):
  # determine if it's a new Job based on GetJob API
  try:
    j: JobProto = nbox_grpc_stub.GetJob(JobRequest(job = job_proto, auth_info=NBXAuthInfo(workspace_id=workspace_id)))
    new_job = j.status in [JobProto.Status.NOT_SET, JobProto.Status.ARCHIVED]
  except grpc.RpcError as e:
    if e.code() == grpc.StatusCode.NOT_FOUND:
      new_job = True
    else:
      raise e

  if not new_job:
    # incase an old job exists, we need to update few things with the new information
    logger.debug("Found existing job, checking for update masks")
    old_job_proto = Job(job_id=job_proto.id, workspace_id = workspace_id).job_proto
    paths = []
    if old_job_proto.resource.SerializeToString(deterministic = True) != job_proto.resource.SerializeToString(deterministic = True):
      paths.append("resource")
    if old_job_proto.schedule.cron != job_proto.schedule.cron:
      paths.append("schedule.cron")
    logger.debug(f"Updating fields: {paths}")
    nbox_grpc_stub.UpdateJob(
      UpdateJobRequest(job = job_proto, update_mask = FieldMask(paths=paths), auth_info=NBXAuthInfo(workspace_id=workspace_id)),
    )

  # update the JobProto with file sizes
  job_proto.code.MergeFrom(Code(
    size = max(int(os.stat(zip_path).st_size / (1024 ** 2)), 1), # jobs in MiB
    type = Code.Type.ZIP,
  ))

  # UploadJobCode is responsible for uploading the code of the job
  response: JobProto = rpc(
    nbox_grpc_stub.UploadJobCode,
    JobRequest(job = job_proto, auth_info=NBXAuthInfo(workspace_id=workspace_id)),
    f"Failed to upload job: {job_proto.id} | {job_proto.name}"
  )
  job_proto.MergeFrom(response)
  s3_url = job_proto.code.s3_url
  s3_meta = job_proto.code.s3_meta
  logger.info(f"Uploading model to S3 ... (fs: {job_proto.code.size/1024/1024:0.3f} MB)")
  r = requests.post(url=s3_url, data=s3_meta, files={"file": (s3_meta["key"], open(zip_path, "rb"))})
  try:
    r.raise_for_status()
  except:
    logger.error(f"Failed to upload model: {r.content.decode('utf-8')}")
    return

  # if this is the first time this is being created
  if new_job:
    job_proto.feature_gates.update({
      "UsePipCaching": "", # some string does not honour value
      "EnableAuthRefresh": ""
    })
    rpc(
      nbox_grpc_stub.CreateJob,
      JobRequest(
        job = job_proto,
        auth_info = NBXAuthInfo(workspace_id = workspace_id, username = secret.get("username"))
      ),
      f"Failed to create job"
    )

  # write out all the commands for this job
  logger.info("Run is now created, to 'trigger' programatically, use the following commands:")
  # _api = f"nbox.Job(id = '{job_proto.id}', workspace_id='{job_proto.auth_info.workspace_id}').trigger()"
  _cli = f"python3 -m nbox jobs --job_id {job_proto.id} --workspace_id {workspace_id} trigger"
  # _curl = f"curl -X POST {secret.get('nbx_url')}/api/v1/workspace/{job_proto.auth_info.workspace_id}/job/{job_proto.id}/trigger"
  _webpage = f"{secret.get('nbx_url')}/workspace/{workspace_id}/jobs/{job_proto.id}"
  # logger.info(f" [python] - {_api}")
  logger.info(f"    [CLI] - {_cli}")
  # logger.info(f"   [curl] - {_curl} -H 'authorization: Bearer $NBX_TOKEN' -H 'Content-Type: application/json' -d " + "'{}'")
  # logger.info(f"   [page] - {_webpage}")
  logger.info(f"See job on page: {_webpage}")

  # create a Job object and return so CLI can do interesting things
  return Job(job_id = job_proto.id, workspace_id = workspace_id)


#######################################################################################################################
"""
# Common

Function related to both NBX-Serving and NBX-Jobs
"""
#######################################################################################################################

def zip_to_nbox_folder(init_folder, id, workspace_id, type, **jinja_kwargs):
  # zip all the files folder
  all_f = U.get_files_in_folder(init_folder, followlinks = False)

  # find a .nboxignore file and ignore items in it
  to_ignore_pat = []
  to_ignore_folder = []
  for f in all_f:
    if f.split("/")[-1] == ".nboxignore":
      with open(f, "r") as _f:
        for pat in _f:
          pat = pat.strip()
          if pat.endswith("/"):
            to_ignore_folder.append(pat)
          else:
            to_ignore_pat.append(pat)
      break

  # print(all_f)

  # print("to_ignore_pat:", to_ignore_pat)
  # print("to_ignore_folder:", to_ignore_folder)

  # two different lists for convinience
  to_remove = []
  for ignore in to_ignore_pat:
    if "*" in ignore:
      x = fnmatch.filter(all_f, ignore)
    else:
      x = [f for f in all_f if f.endswith(ignore)]
    to_remove.extend(x)
  to_remove_folder = []
  for ignore in to_ignore_folder:
    for f in all_f:
      if re.search(ignore, f):
        to_remove_folder.append(f)
  to_remove += to_remove_folder
  all_f = [x for x in all_f if x not in to_remove]
  logger.info(f"Will zip {len(all_f)} files")
  # print(all_f)
  # exit()
  # print(to_remove)
  # exit()

  # zip all the files folder
  zip_path = U.join(gettempdir(), f"nbxjd_{id}@{workspace_id}.nbox")
  logger.info(f"Packing project to '{zip_path}'")
  with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
    abspath_init_folder = os.path.abspath(init_folder)
    for f in all_f:
      arcname = f[len(abspath_init_folder)+1:]
      logger.debug(f"Zipping {f} => {arcname}")
      zip_file.write(f, arcname = arcname)

    # create the exe.py file
    exe_jinja_path = U.join(U.folder(__file__), "assets", "exe.jinja")
    exe_path = U.join(gettempdir(), "exe.py")
    logger.debug(f"Writing exe to: {exe_path}")
    with open(exe_jinja_path, "r") as f, open(exe_path, "w") as f2:
      # get a timestamp like this: Monday W34 [UTC 12 April, 2022 - 12:00:00]
      _ct = datetime.now(timezone.utc)
      _day = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][_ct.weekday()]
      created_time = f"{_day} W{_ct.isocalendar()[1]} [ UTC {_ct.strftime('%d %b, %Y - %H:%M:%S')} ]"

      # fill up the jinja template
      code = jinja2.Template(f.read()).render({
        "created_time": created_time,
        "nbox_version": __version__,
        **jinja_kwargs
      })
      f2.write(code)
    # print(os.stat(exe_path))

    # currently the serving pod does not come with secrets file so we need to make a temporary fix while that
    # feature is being worked on
    if type == OT.SERVING:
      secrets_path = U.join(U.env.NBOX_HOME_DIR(), "secrets.json")
      zip_file.write(secrets_path, arcname = ".nbx/secrets.json")

    zip_file.write(exe_path, arcname = "exe.py")
  return zip_path
