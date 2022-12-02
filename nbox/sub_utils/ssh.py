"""
Creates a socket tunnel between users localhost to server called RSockServer (Reverse Socket Server) .

### Usage

>  nbx tunnel 8000 --i "nbox-dev"
"""

# /Users/yashbonde/Desktop/wrk/nbx/rnd/nbox/nbox/sub_utils/ssh.py:docstring of nbox.sub_utils.ssh:3: ERROR: Unexpected indentation.
# /Users/yashbonde/Desktop/wrk/nbx/rnd/nbox/nbox/sub_utils/ssh.py:docstring of nbox.sub_utils.ssh:4: WARNING: Block quote ends without a blank line; unexpected unindent.
# /Users/yashbonde/Desktop/wrk/nbx/rnd/nbox/nbox/sub_utils/ssh.py:docstring of nbox.sub_utils.ssh.RSockClient:6: ERROR: Unexpected indentation.
# /Users/yashbonde/Desktop/wrk/nbx/rnd/nbox/nbox/sub_utils/ssh.py:docstring of nbox.sub_utils.ssh.RSockClient:19: WARNING: Bullet list ends without a blank line; unexpected unindent.
# /Users/yashbonde/Desktop/wrk/nbx/rnd/nbox/nbox/sub_utils/ssh.py:docstring of nbox.sub_utils.ssh.RSockClient:20: WARNING: Enumerated list ends without a blank line; unexpected unindent.
# /Users/yashbonde/Desktop/wrk/nbx/rnd/nbox/nbox/sub_utils/ssh.py:docstring of nbox.sub_utils.ssh.RSockClient:21: WARNING: Enumerated list ends without a blank line; unexpected unindent.

import os
import sys
import ssl
import socket
import socket
import threading
import subprocess
from time import sleep
from typing import List
from nbox.utils import logger as nbx_logger, FileLogger
from nbox import utils as U
from nbox.auth import secret, ConfigString
from nbox.instance import Instance

class RSockClient:
  # This is a RSockClient. It handels the client socket where client is the user application trying to connect to "client_port"
  # Connects to RSockServer listening on localhost:886.
  # RSockServer recieves instructions as a string and sends back a response.
  # RSockServer requires following steps to setup
  # First,
  #   Authentication:
  #     - Authentication happens by sending
  #       `"AUTH~{AUTH_TOKEN}"`
  #     - AUTH_TOKEN is not defined and is default to 'password'
  #   Seting config:
  #     - you can set config by sending
  #       `"SET_CONFIG~{instance}~{instance_port}"`
  #     - "instance" - Currently is the internal ip of the instance.
  #     - "instance_port" - What port users wants to connect to.
  #   Connect:
  #     - This Starts the main loop which
  #       1. Listen on client_port
  #       2. On connection, 
  #     2. On connection, 
  #       2. On connection, 
  #         a. Send AUTH
  #         b. If AUTH is successful, send SET_CONFIG
  #         c. If SET_CONFIG is successful, send CONNECT
  #         d. If CONNECT is successful, start io_copy
  #   IO_COPY:
  #     - This is the main loop that handles the data transfer between client and server. This is done by creating a new thread for each connection.
  #     - The thread is created by calling the function "io_copy" for each connection that is "server" and "client".
  #     - When a connection is closed, the loop is stopped.

  def __init__(self, connection_id, client_socket, user, subdomain, instance_port, file_logger, auth, secure=False):
    """
    Initializes a reverse sockets client.

    Args:
      client_socket: The socket that the client is connected to.
      instance: The instance that the client wants to connect to.
      instance_port: The port that the instance is listening on.
      auth: The authentication token that the client has to provide to connect to the RSockServer.
      secure: Whether or not the client is using SSL.
    
    """
    self.connection_id = connection_id
    self.client_socket = client_socket
    self.user = user
    self.subdomain = subdomain
    self.instance_port = instance_port
    self.auth = auth
    self.secure = secure

    self.client_auth = False
    self.rsock_thread_running = False
    self.client_thread_running = False
    self.logger = file_logger

    self.log('Starting client')
    self.connect_to_rsock_server()
    self.log('Connected to RSockServer')
    # self.authenticate()
    # self.log('Authenticated client')
    self.set_config()
    self.log('Client init complete')

  def __repr__(self):
    return f"RSockClient({self.subdomain} | {self.connection_id})"
  
  def log(self, message, level="INFO"):
    self.logger.info(f"[{self.connection_id}] [{level}] {message}")
  
  def connect_to_rsock_server(self):
    """
    Connects to RSockServer.
    """
    self.log('Connecting to RSockServer', "DEBUG")
    rsock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    rsock_socket.connect(('rsock.nimblebox.ai', 886))

    if self.secure:
      self.log('Starting SSL')
      certfile = U.join(U.folder(__file__), "pub.crt")
      self.rsock_socket = ssl.wrap_socket(rsock_socket, ca_certs=certfile, cert_reqs=ssl.CERT_REQUIRED)
    else:
      self.rsock_socket = rsock_socket

  def authenticate(self):
    """
    Authenticates the client.
    Sends `"AUTH~{AUTH_TOKEN}"` to RSockServer.
    """
    self.log('Authenticating client')
    self.rsock_socket.sendall(bytes('AUTH~{}'.format(self.auth), 'utf-8'))
    auth = self.rsock_socket.recv(1024)
    auth = auth.decode('utf-8')
    if auth == 'OK':
      self.log('Client authenticated')
    else:
      self.log('Client authentication failed', "ERROR")
      self.client_auth = False
      exit(1)
  
  def set_config(self):
    """
    Sets the config of the client.
    Sends `"SET_CONFIG~{instance}~{instance_port}"` to RSockServer.
    """
    self.log('Setting config')
    self.rsock_socket.sendall(bytes('SET_CLIENT~{}~{}~{}~{}'.format(self.user, self.subdomain, self.instance_port, self.auth), 'utf-8'))
    config = self.rsock_socket.recv(1024)
    config = config.decode('utf-8')
    self.log('Config set to {}'.format(config))
    if config == 'OK':
      self.client_auth = True
      self.log('Config set')
    else:
      self.log('Config set failed', "ERROR")
      exit(1)

  def connect(self):
    """
    Connects the client to RSockServer.
    Sends `"CONNECT"` to RSockServer.
    """
    if self.client_auth:
      self.log('Starting the io_copy loop')
      self.rsock_socket.sendall(bytes('CONNECT', 'utf-8'))

      connect_status = self.rsock_socket.recv(1024)
      connect_status = connect_status.decode('utf-8')
      self.log('Connected status: {}'.format(connect_status))
      if connect_status == 'OK':
        self.log('Connected to project...')
      else:
        self.log('Connect failed', "ERROR")
        exit(1)

      # start the io_copy loop
      self.rsock_thread_running = True
      self.client_thread_running = True

      self.rsock_thread = threading.Thread(target=self.io_copy, args=("server", ))
      self.client_thread = threading.Thread(target=self.io_copy, args=("client", ))

      self.rsock_thread.start()
      self.client_thread.start()
    else:
      self.log('Client authentication failed', "ERROR")
      exit(1)

  def io_copy(self, direction):
    """
    This is the main loop that handles the data transfer between client and server.
    """
    self.log('Starting {} io_copy'.format(direction))

    if direction == 'client':
      client_socket = self.client_socket
      server_socket = self.rsock_socket

    elif direction == 'server':
      client_socket = self.rsock_socket
      server_socket = self.client_socket

    while self.rsock_thread_running and self.client_thread_running:
      try:
        data = client_socket.recv(1024)
        if data:
          # self.log('{} data: {}'.format(direction, data))
          server_socket.sendall(data)
        else:
          self.log('{} connection closed'.format(direction))
          break
      except Exception as e:
        self.log('Error in {} io_copy: {}'.format(direction, e), "ERROR")
        break

    self.rsock_thread_running = False
    self.rsock_thread_running = False
    
    self.log('Stopping {} io_copy'.format(direction))
  
  def stop(self):
    """
    Stops the client.
    """
    self.log('Stopping client')
    self.rsock_thread_running = False
    self.client_thread_running = False
    self.client_socket.close()
    self.rsock_socket.close()
    self.log('Client stopped')


class ConnectionManager:
  def __init__(self, user: str, subdomain: str, file_logger: str, auth: str, notsecure: bool = False):
    """
    Args:
      localport: The port that the client will be listening on.
      user: The user that the client will be connecting as.
      subdomain: The subdomain that the client will be connecting to.
      port: The port that the server will be listening on.
      auth: The build auth token that the client will be using.
      notsecure: Whether or not to use SSL.
    """
    self.user = user
    self.subdomain = subdomain
    self.file_logger = file_logger
    self.auth = auth
    self.notsecure = notsecure
    self.connection_id = 0

    self.done = False
    self.clients = []
    self.threads = []
    self.connections = {}

  def __repr__(self) -> str:
    return f"ConnectionManager({self.subdomain}: {len(self.threads)} Connections)"

  def add(self, localport, buildport, *, _ssh: bool = True):
    """
    Adds a client to the list of clients.
    """
    nbx_logger.debug(f"Creating a connection: [NBX] {buildport} => {localport} [Local]")
    t = threading.Thread(target = self.start, args = (localport, buildport, _ssh))
    t.start()
    self.threads.append(t)
    self.connections[localport] = buildport

  def start(self, localport, buildport, _ssh: bool = True):
    listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if not _ssh:
      listen_socket.settimeout(1.0)
    listen_socket.bind(('localhost', localport))
    listen_socket.listen(20)

    while not self.done:
      nbx_logger.debug("Waiting for connection on port {}".format(localport))
      try:
        client_socket, _ = listen_socket.accept()
      except socket.timeout:
        break

      nbx_logger.debug('Client connected')
      self.connection_id += 1
      nbx_logger.debug(f'Total clients connected -> {self.connection_id}')

      # create the client
      secure = not self.notsecure
      client = RSockClient(self.connection_id, client_socket, self.user, self.subdomain, buildport, self.file_logger, self.auth, secure)
      self.clients.append(client)

      # start the client
      client.connect()
    listen_socket.close()

  def quit(self):
    self.done = False
    sleep(2)
    for client in self.clients:
      client.rsock_socket.close()
    for thread in self.threads:
      thread.join()


def port_in_use(port: int) -> bool:
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    return s.connect_ex(('localhost', port)) == 0


def _create_threads(port: int, *apps_to_ports: List[str], i: str, workspace_id: str, _ssh: bool = True) -> ConnectionManager:
  def _sanity():
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
      raise Exception(f"Unsupported platform '{sys.platform}', raise issue: https://github.com/NimbleBoxAI/nbox/issues")

    # sanity checks because python fire does not handle empty strings
    if i == "":
      raise Exception("Instance name cannot be empty")
    if workspace_id == "":
      raise Exception("Workspace ID cannot be empty")

  _sanity() # run sanity checks

  apps = {} # <localport-cloudport>
  for ap in apps_to_ports:
    try:
      localport, buildport = ap.split(':')
      apps[int(localport)] = int(buildport)
    except ValueError:
      raise ValueError(f"Incorrect local:build '{ap}', are you passing integers?")
  apps[port] = 2222 # hard code

  ports_used = []
  for k in apps:
    if port_in_use(k):
      ports_used.append(str(k))

  if ports_used:
    raise ValueError(f"Ports {', '.join(ports_used)} are already in use")

  # check if instance is the correct one
  instance = Instance(i, workspace_id = workspace_id)
  instance._unopened_error()

  # create logging for RSock
  folder = U.join(U.env.NBOX_HOME_DIR(), "tunnel_logs")
  os.makedirs(folder, exist_ok=True)
  filepath = U.join(folder, f"tunnel_{instance.project_id}.log") # consistency with IDs instead of names
  file_logger = FileLogger(filepath)
  nbx_logger.info(f"Logging to {filepath}")

  # start the instance with _ssh mode
  instance.start(_ssh = True)

  # conman runs threads internally for multiple connections
  conman = ConnectionManager(
    file_logger = file_logger,
    user = secret.get("username"), 
    subdomain = instance.open_data.get("url"),
    auth = instance.open_data.get("token"),
  )
  for localport, buildport in apps.items():
    nbx_logger.debug(f"Creating connection from NBX:{buildport} -> local:{localport}")
    conman.add(localport, buildport, _ssh = _ssh)
  return conman


def tunnel(port: int, *apps_to_ports: List[str], i: str, workspace_id: str = ""):
  """the nbox way to SSH into your instance.

  Usage:
  > nbx tunnel 8000 -i "nbox-dev"

  Args:
    port: Local port for terminal
    *apps_to_ports: A tuple of values `buildport:localport`. For example, ``jupyter:8888`` or ``2001:8002``
    i(str): The instance to connect to
    pwd (str): password to connect to that instance.
  """
  workspace_id: str = workspace_id or secret.get(ConfigString.workspace_id)
  connection = _create_threads(port, *apps_to_ports, i = i, workspace_id = workspace_id)

  try:
    # start the ssh connection on terminal    
    # there is more to know about passing shell = True, see:
    # https://medium.com/python-pandemonium/a-trap-of-shell-true-in-the-subprocess-module-6db7fc66cdfd
    # https://stackoverflow.com/questions/3172470/actual-meaning-of-shell-true-in-subprocess
    nbx_logger.info(f"Starting SSH ... for graceful exit press Ctrl+D then Ctrl+C")
    comm = "ssh"
    if U.env.NBOX_SSH_NO_HOST_CHECKING(False):
      comm += " -o StrictHostKeychecking=no"
    comm += f" -p {port} ubuntu@localhost"
    nbx_logger.debug(f"Running command: {comm}")
    subprocess.call(comm, shell=True)
  except KeyboardInterrupt:
    nbx_logger.info("KeyboardInterrupt, closing connections")
    connection.quit()

  connection.quit()
  sys.exit(0) # graceful exit