"""
NimbleBox LMAO is our general purpose observability tool for any kind of computation you might have.
"""

# f"{lmao.project_id}/{lmao.run.experiment_id}/model.pkl"
# drift detection and all
# run.log_dataset(
#     dataset_name='train',
#     features=X_train,
#     predictions=y_pred_train,
#     actuals=y_train,
# )
# run.log_dataset(
#     dataset_name='test',
#     features=X_test,
#     predictions=y_pred_test,
#     actuals=y_test,
# )

import os
import re
import sys
import shlex
import zipfile
from git import Repo
from json import dumps
from functools import lru_cache
from requests import Session
from typing import Dict, Any, List, Optional, Union
from subprocess import Popen
from google.protobuf.field_mask_pb2 import FieldMask

import nbox.utils as U
from nbox import Instance
from nbox.utils import logger, SimplerTimes
from nbox.auth import secret, ConfigString
from nbox.nbxlib.tracer import Tracer
from nbox.relics import RelicsNBX
from nbox.jobs import Job, upload_job_folder
from nbox.hyperloop.common_pb2 import Resource
from nbox.init import nbox_grpc_stub
from nbox.messages import message_to_dict
from nbox.hyperloop.nbox_ws_pb2 import UpdateJobRequest
from nbox.hyperloop.job_pb2 import Job as JobProto
from nbox.hyperloop.common_pb2 import NBXAuthInfo

# all the sublime -> hyperloop stuff
from nbox.sublime.lmao_rpc_client import (
  LMAO_Stub, # main stub class
  Record, File, FileList, AgentDetails, RunLog,
  Run, InitRunRequest, ListProjectsRequest
)
from nbox.observability.system import SystemMetricsLogger


"""
functional components of LMAO
"""

@lru_cache()
def get_lmao_stub(username: str, workspace_id: str) -> LMAO_Stub:
  # prepare the URL
  id_or_name = f"monitoring-{workspace_id}"
  logger.info(f"Instance id_or_name: {id_or_name}")
  logger.debug(f"workspace_id: {workspace_id}")
  instance = Instance(id_or_name, workspace_id = workspace_id)
  try:
    open_data = instance.open_data
  except AttributeError:
    raise Exception(f"Is instance '{instance.project_id}' running?")
  build = "build"
  if "app.c." in secret.get("nbx_url"):
    build = "build.c"
  url = f"https://server-{open_data['url']}.{build}.nimblebox.ai/"
  logger.debug(f"URL: {url}")

  # create a session with the auth header
  _session = Session()
  _session.headers.update({
    "NBX-TOKEN": open_data["token"],
    "X-NBX-USERNAME": username,
  })

  # define the stub
  # self.lmao = LMAO_Stub(url = "http://127.0.0.1:8080", session = Session()) # debug
  lmao_stub = LMAO_Stub(url = url, session = _session)
  return lmao_stub

def get_record(k: str, v: Union[int, float, str]) -> Record:
  """Function to create a Record protobuf object from a key and value."""
  _tv = type(v)
  assert _tv in [int, float, str], f"[key = {k}] '{_tv}' is not a valid type"
  _vt = {
    int: Record.DataType.INTEGER,
    float: Record.DataType.FLOAT,
    str: Record.DataType.STRING,
  }[_tv]
  record = Record(key = k, value_type = _vt)
  if _tv == int:
    record.integer_data.append(v)
  elif _tv == float:
    record.float_data.append(v)
  elif _tv == str:
    record.string_data.append(v)
  return record

def get_git_details(folder):
  """If there is a .git folder in the folder, return some details for that."""
  repo = Repo(folder)

  # check for any unstaged files
  uncommited_files = {}
  diff = repo.index.diff(None)
  for f in diff:
    path = f.a_path or f.b_path # when new file is added, a_path is None
    uncommited_files[path] = f.change_type
  if uncommited_files:
    logger.warning(f"Uncommited files: {uncommited_files}")

  # get the remote url
  try:
    remote_url = repo.remote().url

    # clean the remote_url because it can sometimes contain the repo token as well.
    # this can become security hazard. so if you have an example, that is not suppported:
    #   go ahead, make a PR!
    if "github.com" in remote_url:
      remote_url = re.sub(r"ghp_\w+@", "", remote_url)
  except ValueError:
    remote_url = None

  # get the size of the repository
  size = None
  for line in repo.git.count_objects("-v").splitlines():
    if line.startswith("size:"):
      size = int(line[len("size:") :].strip())
  if size > (1 << 30):
    logger.warning(f"Repository size over 1GB, you might want to work on it")

  return {
    "remote_url": remote_url,
    "branch": repo.active_branch.name,
    "commit": repo.head.commit.hexsha,
    "uncommited_files": uncommited_files,
    "untracked_files": repo.untracked_files,
    "size": size,
  }

"""
Client library that the user will use to interact with the LMAO server.
"""

class _lmaoConfig:
  kv = {}
  def set(
    workspace_id: str = "",
    project_id: Optional[str] = "",
    project_name: Optional[str] = "",
    experiment_id: Optional[str] = "",
    metadata: Dict[str, Any] = {},
    save_to_relic: bool = False,
    enable_system_monitoring: bool = False,
    store_git_details: bool = True,
    args = (),
    kwargs = {},
  ) -> None:
    _lmaoConfig.kv = {
      "workspace_id": workspace_id,
      "project_id": project_id,
      "project_name": project_name,
      "experiment_id": experiment_id,
      "metadata": metadata,
      "save_to_relic": save_to_relic,
      "enable_system_monitoring": enable_system_monitoring,
      "store_git_details": store_git_details,
      "args": args,
      "kwargs": kwargs,
    }

  def clear() -> None:
    _lmaoConfig.kv = {}

  def json() -> str:
    # return dumps({k:v for k,v in _lmaoConfig.kv.items() if k not in ["args", "kwargs"]})
    return dumps(_lmaoConfig.kv)


class Lmao():
  def __init__(
    self,
    project_name: Optional[str] = "",
    project_id: Optional[str] = "",
    experiment_id: Optional[str] = "",
    metadata: Dict[str, Any] = {},
    save_to_relic: bool = False,
    enable_system_monitoring: bool = False,
    store_git_details: bool = True,
    workspace_id: str = "",
  ) -> None:
    """`Lmao` is the client library for using NimbleBox Monitoring. It talks to your monitoring instance running on your build
    and stores the information in the `project_name` or `project_id`. This object inherently doesn't care what you are actually
    logging and rather concerns itself with ensuring storage.

    **Note**: All arguments are optional, if the _lmaoConfig is set.
    
    Args:
      project_name (str, optional): The name of the project. Defaults to "".
      project_id (str, optional): The id of the project. Defaults to "".
      experiment_id (str, optional): The id of the experiment. Defaults to "".
      metadata (Dict[str, Any], optional): Any metadata that you want to store. Defaults to {}.
      save_to_relic (bool, optional): Whether to save the data to the relic. Defaults to False.
      enable_system_monitoring (bool, optional): Whether to enable system monitoring. Defaults to False.
      store_git_details (bool, optional): Whether to store git details. Defaults to True.
      workspace_id (str, optional): The id of the workspace. Defaults to "".
    """

    self.config = _lmaoConfig.kv

    if _lmaoConfig.kv:
      # load all the values from the config
      project_name = _lmaoConfig.kv["project_name"]
      project_id = _lmaoConfig.kv["project_id"]
      experiment_id = _lmaoConfig.kv["experiment_id"]
      metadata = _lmaoConfig.kv["metadata"]
      save_to_relic = _lmaoConfig.kv["save_to_relic"]
      enable_system_monitoring = _lmaoConfig.kv["enable_system_monitoring"]
      store_git_details = _lmaoConfig.kv["store_git_details"]
      workspace_id = _lmaoConfig.kv["workspace_id"]

    self.project_name = project_name
    self.project_id = project_id
    self.experiment_id = experiment_id
    self.metadata = metadata
    self.save_to_relic = save_to_relic
    self.enable_system_monitoring = enable_system_monitoring
    self.store_git_details = store_git_details
    self.workspace_id = workspace_id or secret.get(ConfigString.workspace_id)
    
    # now set the supporting keys
    self.nbx_job_folder = U.env.NBOX_JOB_FOLDER("")
    self._total_logged_elements = 0 # this variable keeps track for logging
    self.completed = False
    self.tracer = None
    self.username = secret.get("username")
    self.relic: RelicsNBX = None
    self.system_monitoring: SystemMetricsLogger = None
    self._nbx_run_id = None
    self._nbx_job_id = None

    # create connection and initialise the run
    self._init(project_name, project_id, config = metadata)

  def __del__(self):
    if self.system_monitoring is not None:
      self.system_monitoring.stop()

  def _get_name_id(self, project_id: str = None, project_name: str = None):
    matching_projects = self.lmao.list_projects(
      ListProjectsRequest(
        workspace_id = self.workspace_id,
        project_id_or_name = project_id if project_id else project_name,
      )
    )

    # case for one matching project
    if len(matching_projects.projects) == 1:
      project_name = matching_projects.projects[0].project_name
      project_id = matching_projects.projects[0].project_id
    elif len(matching_projects.projects) > 1:
      if project_id:
        logger.error(f"Multiple entries for {project_id} found, something went wrong from our side.")
        raise Exception(f"Database duplicate entry for project_id: {project_id}")
      else:
        logger.error(f"Project '{project_name}' found multiple times, please use project_id instead.")
        raise Exception("Ambiguous project name, please use project_id instead.")
    else:
      # case for no matching projects returned
      # means we have to initialise this project first and then we will pull the details after the init_run
      if project_id:
        raise Exception(f"Project with id {project_id} not found, please create a new one with project_name")
      logger.info(f"Project '{project_name}' not found, will create a new one.")

    return project_id, project_name

  def _init(self, project_name, project_id, config: Dict[str, Any] = {}):
    # create a tracer object that will load all the information
    tracer = Tracer(start_heartbeat = False)
    self._nbx_run_id = tracer.run_id
    self._nbx_job_id = tracer.job_id
    del tracer
    self.lmao = get_lmao_stub(self.username, self.workspace_id)

    # do a quick lookup and see if the project exists, if not, create it
    if self.config:
      project_id = self.config["project_id"]
      project_name = self.config["project_name"]
    else:
      if project_id:
        project_id, project_name = self._get_name_id(project_id=project_id)
        if not project_id:
          raise Exception(f"Project with id {project_id} not found, please create a new one with project_name")
      elif project_name:
        project_id, project_name = self._get_name_id(project_name=project_name)
        if not project_id:
          logger.info(f"Project '{project_name}' not found, create one from dashboard.")
      else:
        raise Exception("provide either `project_id` or `project_name`")

    # this is the config value that is used to store data on the plaform, user cannot be allowed to have
    # like a full access to config values
    log_config: Dict[str, Any] = {
      "user_config": config
    }

    # check if the current folder from where this code is being executed has a .git folder
    # NOTE: in case of NBX-Jobs the current folder ("./") is expected to contain git by default
    log_config["git"] = None
    if os.path.exists(".git") and self.store_git_details:
      log_config["git"] = get_git_details("./")

    # continue as before
    self._agent_details = AgentDetails(
      type = AgentDetails.NBX.JOB,
      nbx_job_id = self._nbx_job_id or "",
      nbx_run_id = self._nbx_run_id or "",
    )

    if self.experiment_id:
      run_details = self.lmao.get_run_details(Run(experiment_id = self.experiment_id, project_id=project_id))
      if not run_details:
        # TODO: Make a custom exception of this
        raise Exception("Server Side exception has occurred, Check the log for details")
      if run_details.experiment_id:
        # means that this run already exists so we need to make an update call
        ack = self.lmao.update_run_status(Run(
          project_id = project_id,
          experiment_id = run_details.experiment_id,
          agent = self._agent_details,
          update_keys = ["agent"],
        ))
        if not ack.success:
          raise Exception(f"Failed to update run status! {ack.message}")
    else:
      run_details = self.lmao.init_run(
        InitRunRequest(
          agent_details=self._agent_details,
          created_at = SimplerTimes.get_now_i64(),
          project_name = project_name,
          project_id = project_id,
          config = dumps(log_config),
        )
      )

    self.project_name = project_name
    self.project_id = run_details.project_id
    self.metadata = config

    self.run = run_details
    logger.info(f"Created a new LMAO run")
    logger.info(f" project: {self.project_name} ({self.project_id})")
    logger.info(f"      id: {self.run.experiment_id}")
    logger.info(f"    link: https://app.nimblebox.ai/workspace/{self.workspace_id}/monitoring/{self.project_id}/{self.run.experiment_id}")

    # now initialize the relic
    if self.save_to_relic:
      # The relic will be the project id
      self.relic = RelicsNBX("experiments", self.workspace_id, create = True)
      logger.info(f"Will store everything in folder: {self.experiment_prefix}")

    # system metrics monitoring, by default is enabled optionally turn it off
    if self.enable_system_monitoring:
      self.system_monitoring = SystemMetricsLogger(self)
      self.system_monitoring.start()

  @property
  def experiment_prefix(self):
    prefix = f"{self.project_name}/{self.run.experiment_id}/"
    return prefix

  """The functions below are the ones supposed to be used."""

  @lru_cache(maxsize=1)
  def get_relic(self):
    """Get the underlying Relic for more advanced usage patterns."""
    return RelicsNBX("experiments", self.workspace_id, create = True, prefix = f"{self.project_name}/{self.run.experiment_id}")

  def log(self, y: Dict[str, Union[int, float, str]], step = None, *, log_type: str = RunLog.LogType.USER):
    """Log a single level dictionary to the platform at any given step. This function does not really care about the
    information that is being logged, it just logs it to the platform."""
    if self.completed:
      raise Exception("Run already completed, cannot log more data!")

    step = step if step is not None else SimplerTimes.get_now_i64()
    if step < 0:
      raise Exception("Step must be <= 0")
    run_log = RunLog(experiment_id = self.run.experiment_id, project_id=self.project_id, log_type=log_type)
    for k,v in y.items():
      # TODO:@yashbonde replace Record with RecordColumn
      record = get_record(k, v)
      record.step = step
      run_log.data.append(record)

    ack = self.lmao.on_log(run_log)
    if not ack.success:
      logger.error("  >> Server Error")
      for l in ack.message.splitlines():
        logger.error("  " + l)
      raise Exception("Server Error")

    self._total_logged_elements += 1

  def save_file(self, *files: List[str]):
    """
    Register a file save. User should be aware of some structures that we follow for standardizing the data.
    All the experiments are going to be tracked under the following pattern:

    1. `relic_name` is going to be the experiment ID, so any changes to the name will not affect relic storage
    2. `{experiment_id}(_{job_id}@{experiment_id})` is the name of the folder which contains all the artifacts in the experiment.

    If relics is not enabled, this function will simply log to the LMAO DB.

    dk.save_file("foo.t", "/bar/", "baz.t", "/bar/roo/")
    """
    logger.info(f"Saving files: {files}")

    # manage all the complexity of getting the list of RelicFile
    all_files = []
    for folder_or_file in files:
      if os.path.isfile(folder_or_file):
        all_files.append(folder_or_file)
      elif os.path.isdir(folder_or_file):
        all_files.extend(U.get_files_in_folder(folder_or_file, self.username, self.workspace_id))
      else:
        raise Exception(f"File or Folder not found: {folder_or_file}")

    logger.debug(f"Storing {len(all_files)} files")
    if self.save_to_relic:
      relic = self.get_relic()
      logger.info(f"Uploading files to relic: {relic}")
      for f in all_files:
        relic.put(f)

    # TODO: @yashbonde log the files in the LMAO DB for sanity, currently this is a no-op, keeping it here so one day
    # when we add something cool we can use this
    # fl = FileList(experiment_id = self.run.experiment_id)
    # fl.files.extend([File(relic_file = RelicFile(name = x)) for x in all_files])
    # self.lmao.on_save(fl)

  def end(self):
    """End the run to declare it complete. This is more of a convinience function than anything else. For example when you
    are monitoring a live API you may never know when the experiment is complete. This function locks the experiment name
    so it can't be modified."""
    if self.completed:
      logger.error("Run already completed, cannot end it again!")
      return None

    logger.info("Ending run")
    ack = self.lmao.on_train_end(self.run)
    if not ack.success:
      logger.error("  >> Server Error")
      for l in ack.message.splitlines():
        logger.error("  " + l)
      raise Exception("Server Error")
    self.completed = True
    if self.enable_system_monitoring:
      self.system_monitoring.stop()

"""
For some Experiences you want to have CLI control so this class manages that

lmao upload fp:fn project_name_or_id
lmao trigger project_name_or_id
lmao open project_name_or_id
"""

@lru_cache()
def get_project_name_id(project_name_or_id: str, workspace_id: str):
  username = secret.get("username")
  lmao_stub = get_lmao_stub(username, workspace_id)
  out = lmao_stub.list_projects(ListProjectsRequest(workspace_id=workspace_id, project_id_or_name=project_name_or_id))
  if not out.projects:
    logger.error(f"Project: {project_name_or_id} not found")
    logger.error(f"Will automatically create one")
    name = project_name_or_id
    id = None
  elif len(out.projects) > 1:
    raise ValueError(f"Multiple projects found for: {project_name_or_id}")
  else:
    p = out.projects[0]
    name = p.project_name
    id = p.project_id
  return name, id


class LmaoCLI:
  # This class will only manage the things that need to talk to the DB and everything else is offloaded
  def upload(
    self,
    init_path: str,
    project_name_or_id: str,
    workspace_id: str = "",
    trigger: bool = False,

    # all the arguments for the git thing
    untracked: bool = False,
    untracked_no_limit: bool = False,

    # all the things for resources
    resource_cpu: str = "100m",
    resource_memory: str = "128Mi",
    resource_disk_size: str = "1Gi",
    resource_gpu: str = "none",
    resource_gpu_count: str = "0",
    resource_timeout: int = 120_000,
    resource_max_retries: int = 2,

    # the following arguments are used for the initialisation of lmao class
    save_to_relic: bool = True,
    enable_system_monitoring: bool = False,

    # the following things are needed for the different modules in the process
    relics_kwargs: Dict[str, Any] = {},

    # any other things to pass to the function / class being called
    **run_kwargs
  ):
    """Upload and register a new run for a NBX-LMAO project.

    Args:
      init_path (str): This can be a path to a `folder` or can be optionally of the structure `fp:fn` where `fp`
        is the path to the file and `fn` is the function name.
      project_name_or_id (str): The name or id of the LMAO project.
      workspace_id (str, optional): If nor provided, defaults to global config
      trigger (bool, optional): Defaults to False. If True, will trigger the run after uploading.
      untracked (bool, optional): If True, then untracked files below 1MB will also be zipped and uploaded. Defaults to False.
      untracked_no_limit (bool, optional): If True, then all untracked files will also be zipped and uploaded. Defaults to False.
      resource_cpu (str, optional): Defaults to "100m". The CPU resource to allocate to the run.
      resource_memory (str, optional): Defaults to "128Mi". The memory resource to allocate to the run.
      resource_disk_size (str, optional): Defaults to "1Gi". The disk size resource to allocate to the run.
      resource_gpu (str, optional): Defaults to "none". The GPU resource to use.
      resource_gpu_count (str, optional): Defaults to "0". Number of GPUs allocated to the experiment.
      resource_timeout (int, optional): Defaults to 120_000. The timeout between two consecutive runs, honoured but not guaranteed.
      resource_max_retries (int, optional): Defaults to 2. The maximum number of retries for a run.
      **run_kwargs: These are the kwargs that will be passed to your Operator.
    """
    # reconstruct the entire CLI command so we can show it in the UI
    workspace_id = workspace_id or secret.get(ConfigString.workspace_id)
    reconstructed_cli_comm = (
      f"nbx lmao upload '{init_path}' '{project_name_or_id}'"
      f" --workspace_id '{workspace_id}'"
      f" --resource_cpu '{resource_cpu}'"
      f" --resource_memory '{resource_memory}'"
      f" --resource_disk_size '{resource_disk_size}'"
      f" --resource_gpu '{resource_gpu}'"
      f" --resource_gpu_count '{resource_gpu_count}'"
      f" --resource_timeout {resource_timeout}"
      f" --resource_max_retries {resource_max_retries}"
    )
    if trigger:
      reconstructed_cli_comm += " --trigger"
    if untracked:
      reconstructed_cli_comm += " --untracked"
    if untracked_no_limit:
      reconstructed_cli_comm += " --untracked_no_limit"
    if save_to_relic:
      reconstructed_cli_comm += " --save_to_relic"
    if enable_system_monitoring:
      reconstructed_cli_comm += " --enable_system_monitoring"

    if resource_max_retries < 1:
      logger.error(f"max_retries must be >= 1. Got: {resource_max_retries}\n  Fix: set --max_retries=2")
      raise ValueError()

    # uncommenting these since it can contain sensitive information, kept just in case for taking dictionaries as input
    # if relics_kwargs:
    #   rks = str(relics_kwargs).replace("'", "")
    #   reconstructed_cli_comm += f" --relics_kwargs '{rks}'"
    
    for k, v in run_kwargs.items():
      if type(v) == bool:
        reconstructed_cli_comm += f" --{k}"
      else:
        reconstructed_cli_comm += f" --{k} '{v}'"
    logger.debug(f"command: {reconstructed_cli_comm}")

    # clean user args
    resource_gpu_count = str(resource_gpu_count)
    if untracked_no_limit and not untracked:
      logger.debug("untracked_no_limit is True but untracked is False. Setting untracked to True")
      untracked = True

    # first step is to get all the relevant information from the DB
    workspace_id = workspace_id or secret.get(ConfigString.workspace_id)
    project_name, project_id = get_project_name_id(project_name_or_id, workspace_id)
    logger.info(f"Project: {project_name} ({project_id})")

    # create a call init run and get the experiment metadata
    init_folder, _ = os.path.split(init_path)
    init_folder = init_folder or "."
    if os.path.exists(U.join(init_folder, ".git")):
      git_det = get_git_details(init_folder)
    else:
      git_det = {}


    job_name = "nbxj_" + project_name[:15]
    job = Job(job_name = job_name, workspace_id = workspace_id)
    lmao_stub = get_lmao_stub(secret.get("username"), workspace_id)

    _metadata = {
      "user_config": run_kwargs,
      "git": git_det,
      "resource": message_to_dict(Resource(
        cpu = resource_cpu,
        memory = resource_memory,
        disk_size = resource_disk_size,
        gpu = resource_gpu,
        gpu_count = resource_gpu_count,
        timeout = resource_timeout,
        max_retries = resource_max_retries,
      )),
      "cli": reconstructed_cli_comm,
      "lmao": {
        "save_to_relic": save_to_relic,
        "enable_system_monitoring": enable_system_monitoring,
      }
    }

    run = lmao_stub.init_run(InitRunRequest(
      agent_details = AgentDetails(
        type = AgentDetails.NBX.JOB,
        nbx_job_id = job.id, # run id will have to be updated from the job
      ),
      created_at = SimplerTimes.get_now_i64(),
      config = dumps(_metadata),
      project_id = project_id,
      project_name = project_name,
    ))
    if not project_id:
      logger.info(f"Project {project_name} created with id: {run.project_id}")
    logger.info(f"Run ID: {run.experiment_id}")

    # connect to the relic
    r_keys = set(relics_kwargs.keys())
    valid_keys = {"bucket_name", "region", "nbx_resource_id", "nbx_integration_token"}
    extra_keys = r_keys - valid_keys
    if extra_keys:
      logger.error("Unknown arguments found:\n  * " + "\n  * ".join(extra_keys))
      raise RuntimeError("Unknown arguments found in the Relic")
    relic = RelicsNBX(LMAO_RELIC_NAME, workspace_id = workspace_id, create = True, **relics_kwargs)

    # create a git patch and upload it to relics
    if git_det:
      _zf = U.join(init_folder, "untracked.zip")
      zip_file = zipfile.ZipFile(_zf, "w", zipfile.ZIP_DEFLATED)
      patch_file = U.join(init_folder, "nbx_auto_patch.diff")
      f = open(patch_file, "w")
      Popen(shlex.split(f"git diff {' '.join(git_det['uncommited_files'])}"), stdout=f, stderr=sys.stderr).wait()
      f.close()
      zip_file.write(patch_file, arcname = patch_file)
      if untracked:
        untracked_files = git_det["untracked_files"] # what to do with these?
        if untracked_files:
          # see if any file is larger than 10MB and if so, warn the user
          warn_once = False
          for f in untracked_files:
            if os.path.getsize(f) > 1e7 and not untracked_no_limit:
              logger.warning(f"File: {f} is larger than 10MB and will not be available in sync")
              logger.warning("  Fix: use git to track small files, avoid large files")
              logger.warning("  Fix: nbox.Relics can be used to store large files")
              logger.warning("  Fix: use --untracked-no-limit to upload all files")
              warn_once = True
              continue
            zip_file.write(f, arcname = f)
      relic.put_to(_zf, f"{project_name}/{run.experiment_id}/git.zip")
      os.remove(_zf)
      os.remove(patch_file)

    # tell the server that this run is being scheduled so atleast the information is visible on the dashboard
    upload_job_folder(
      "job",
      init_folder = init_path,
      name = "nbxj_" + project_name[:15], # keep only 20 chars

      # pass along the resource requirements
      resource_cpu = resource_cpu,
      resource_memory = resource_memory,
      resource_disk_size = resource_disk_size,
      resource_gpu = resource_gpu,
      resource_gpu_count = resource_gpu_count,
      resource_timeout = resource_timeout,
      resource_max_retries = resource_max_retries,
    )

    if trigger:
      logger.debug(f"Running job '{job.name}' ({job.id})")

      # create the serialisable config
      _lmaoConfig.clear()
      _lmaoConfig.set(
        workspace_id = workspace_id,
        project_name = run.project_name,
        project_id = run.project_id,
        experiment_id = run.experiment_id,
        metadata = _metadata,
        save_to_relic = save_to_relic,
        enable_system_monitoring = enable_system_monitoring,
        args = (),
        kwargs = run_kwargs,
      )
      
      # from pprint import pprint
      # pprint(_lmaoConfig.kv)
      # exit()

      # put the items in the relic and create a tag from it
      fp = f"{project_name}/{run.experiment_id}"
      relic.put_object(fp+"/init.pkl", _lmaoConfig.kv)
      tag = f"{LMAO_JOB_TYPE_PREFIX}-{fp}"
      logger.info(f"Run tag: {tag}")
      job.trigger(tag)

      # clear so the rest of the program doesn't get affected
      _lmaoConfig.clear()

    # finally print the location of the run where the users can track this
    logger.info(f"Run location: https://app.nimblebox.ai/workspace/{workspace_id}/monitoring/{project_id}/{run.experiment_id}")

# do not change these it can become a huge pain later on
LMAO_RELIC_NAME = "experiments"
LMAO_JOB_TYPE_PREFIX = "NBXLmao"
LMAO_ENV_VAR_PREFIX = "LMAO_"
