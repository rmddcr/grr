#!/usr/bin/env python
"""These flows are designed for high performance transfers."""


import zlib

import logging
from grr.lib import aff4
from grr.lib import config_lib
from grr.lib import data_store
from grr.lib import flow
from grr.lib import rdfvalue
from grr.lib.aff4_objects import aff4_grr
from grr.lib.aff4_objects import collects
from grr.lib.aff4_objects import filestore
from grr.lib.rdfvalues import client as rdf_client
from grr.lib.rdfvalues import crypto as rdf_crypto
from grr.lib.rdfvalues import flows as rdf_flows
from grr.lib.rdfvalues import paths as rdf_paths
from grr.lib.rdfvalues import protodict as rdf_protodict
from grr.lib.rdfvalues import structs as rdf_structs
from grr.proto import flows_pb2


class GetFileArgs(rdf_structs.RDFProtoStruct):
  protobuf = flows_pb2.GetFileArgs


class GetFile(flow.GRRFlow):
  """An efficient file transfer mechanism (deprecated, use MultiGetFile).

  This flow is deprecated in favor of MultiGetFile, but kept for now for use by
  MemoryCollector since the buffer hashing performed by MultiGetFile is
  pointless for memory acquisition.

  GetFile can also retrieve content from device files that report a size of 0 in
  stat when read_length is specified.

  Returns to parent flow:
    An PathSpec.
  """

  category = "/Filesystem/"

  args_type = GetFileArgs

  # We have a maximum of this many chunk reads outstanding (about 10mb)
  WINDOW_SIZE = 200
  CHUNK_SIZE = 512 * 1024

  @classmethod
  def GetDefaultArgs(cls, token=None):
    _ = token
    result = cls.args_type()
    result.pathspec.pathtype = "OS"

    return result

  @flow.StateHandler()
  def Start(self):
    """Get information about the file from the client."""
    self.state.max_chunk_number = max(2,
                                      self.args.read_length / self.CHUNK_SIZE)

    self.state.current_chunk_number = 0
    self.state.file_size = 0
    self.state.blobs = []
    self.state.stat_entry = None

    self.CallClient(
        "StatFile",
        rdf_client.ListDirRequest(pathspec=self.args.pathspec),
        next_state="Stat")

  @flow.StateHandler()
  def Stat(self, responses):
    """Fix up the pathspec of the file."""
    response = responses.First()
    if responses.success and response:
      self.state.stat_entry = response
    else:
      if not self.args.ignore_stat_failure:
        raise IOError("Error: %s" % responses.status)

      # Just fill up a bogus stat entry.
      self.state.stat_entry = rdf_client.StatEntry(pathspec=self.args.pathspec)

    # Adjust the size from st_size if read length is not specified.
    if self.args.read_length == 0:
      self.state.file_size = self.state.stat_entry.st_size
    else:
      self.state.file_size = self.args.read_length

    self.state.max_chunk_number = (self.state.file_size / self.CHUNK_SIZE) + 1

    self.FetchWindow(
        min(self.WINDOW_SIZE, self.state.max_chunk_number - self.state[
            "current_chunk_number"]))

  def FetchWindow(self, number_of_chunks_to_readahead):
    """Read ahead a number of buffers to fill the window."""
    for _ in range(number_of_chunks_to_readahead):

      # Do not read past the end of file
      next_offset = self.state.current_chunk_number * self.CHUNK_SIZE
      if next_offset >= self.state.file_size:
        return

      request = rdf_client.BufferReference(
          pathspec=self.args.pathspec,
          offset=next_offset,
          length=self.CHUNK_SIZE)
      self.CallClient("TransferBuffer", request, next_state="ReadBuffer")
      self.state.current_chunk_number += 1

  @flow.StateHandler()
  def ReadBuffer(self, responses):
    """Read the buffer and write to the file."""
    # Did it work?
    if responses.success:
      response = responses.First()
      if not response:
        raise IOError("Missing hash for offset %s missing" % response.offset)

      if response.offset <= self.state.max_chunk_number * self.CHUNK_SIZE:
        # Response.data is the hash of the block (32 bytes) and
        # response.length is the length of the block.
        self.state.blobs.append((response.data, response.length))
        self.Log("Received blob hash %s", response.data.encode("hex"))

        # Add one more chunk to the window.
        self.FetchWindow(1)

      if response.offset + response.length >= self.state.file_size:
        # File is complete.
        stat_entry = self.state.stat_entry
        urn = aff4_grr.VFSGRRClient.PathspecToURN(
            self.state.stat_entry.pathspec, self.client_id)

        stat_entry.aff4path = urn
        with aff4.FACTORY.Create(
            urn, aff4_grr.VFSBlobImage, token=self.token) as fd:
          fd.SetChunksize(self.CHUNK_SIZE)
          fd.Set(fd.Schema.STAT(stat_entry))

          for data, length in self.state.blobs:
            fd.AddBlob(data, length)
            fd.Set(fd.Schema.CONTENT_LAST, rdfvalue.RDFDatetime().Now())

          # Save some space.
          del self.state.blobs

        self.state.success = True

  @flow.StateHandler()
  def End(self):
    """Finalize reading the file."""
    if not self.state.get("success"):
      self.Log("File transfer failed.")
      self.Notify("ViewObject", self.client_id, "File transfer failed.")
    else:
      stat_entry = self.state.stat_entry
      self.Log("File %s transferred successfully.", stat_entry.aff4path)
      self.Notify("ViewObject", stat_entry.aff4path,
                  "File transferred successfully.")

      # Notify any parent flows the file is ready to be used now.
      self.SendReply(stat_entry)

    super(GetFile, self).End()


class MultiGetFileMixin(object):
  """A flow mixin to efficiently retrieve a number of files.

  The class extending this can provide a self.state with the following
  attributes:
  - file_size: int. Maximum number of bytes to download.
  - use_external_stores: boolean. If true, look in any defined external file
    stores for files before downloading them, and offer any new files to
    external stores. This should be true unless the external checks are
    misbehaving.
  """

  CHUNK_SIZE = 512 * 1024

  # Batch calls to the filestore to at least to group this many items. This
  # allows us to amortize file store round trips and increases throughput.
  MIN_CALL_TO_FILE_STORE = 200

  def Start(self,
            file_size=0,
            maximum_pending_files=1000,
            use_external_stores=False):
    """Initialize our state."""
    super(MultiGetFileMixin, self).Start()

    self.state.files_hashed = 0
    self.state.use_external_stores = use_external_stores
    self.state.file_size = file_size
    self.state.files_to_fetch = 0
    self.state.files_fetched = 0
    self.state.files_skipped = 0

    # Counter to batch up hash checking in the filestore
    self.state.files_hashed_since_check = 0

    # A dict of file trackers which are waiting to be checked by the file
    # store.  Keys are vfs urns and values are FileTrack instances.  Values are
    # copied to pending_files for download if not present in FileStore.
    self.state.pending_hashes = {}

    # A dict of file trackers currently being fetched. Keys are vfs urns and
    # values are FileTracker instances.
    self.state.pending_files = {}

    # The maximum number of files we are allowed to download concurrently.
    self.state.maximum_pending_files = maximum_pending_files

    # As pathspecs are added to the flow they are appended to this array. We
    # then simply pass their index in this array as a surrogate for the full
    # pathspec. This allows us to use integers to track pathspecs in dicts etc.
    self.state.indexed_pathspecs = []

    # The index of the next pathspec to start. Pathspecs are added to
    # indexed_pathspecs and wait there until there are free trackers for
    # them. When the number of pending_files falls below the
    # "maximum_pending_files" count] = we increment this index and start of
    # downloading another pathspec.
    self.state.next_pathspec_to_start = 0

    # Set of blobs we still need to fetch.
    self.state.blobs_we_need = set()

  def StartFileFetch(self, pathspec, request_data=None):
    """The entry point for this flow mixin - Schedules new file transfer."""
    # Create an index so we can find this pathspec later.
    self.state.indexed_pathspecs.append((pathspec, request_data or {}))
    self._TryToStartNextPathspec()

  def _TryToStartNextPathspec(self):
    """Try to schedule the next pathspec if there is enough capacity."""
    # Nothing to do here.
    if self.state.maximum_pending_files <= len(self.state.pending_files):
      return

    if self.state.maximum_pending_files <= len(self.state.pending_hashes):
      return

    try:
      index = self.state.next_pathspec_to_start
      pathspec = self.state.indexed_pathspecs[index][0]
      self.state.next_pathspec_to_start = index + 1
    except IndexError:
      # We did all the pathspecs, nothing left to do here.
      return

    # Add the file tracker to the pending hashes list where it waits until the
    # hash comes back.
    self.state.pending_hashes[index] = {"index": index}

    # First state the file, then hash the file.
    self.CallClient(
        "StatFile",
        pathspec=pathspec,
        next_state="StoreStat",
        request_data=dict(index=index))

    request = rdf_client.FingerprintRequest(
        pathspec=pathspec, max_filesize=self.state.file_size)
    request.AddRequest(
        fp_type=rdf_client.FingerprintTuple.Type.FPT_GENERIC,
        hashers=[rdf_client.FingerprintTuple.HashType.MD5,
                 rdf_client.FingerprintTuple.HashType.SHA1,
                 rdf_client.FingerprintTuple.HashType.SHA256])

    self.CallClient(
        "HashFile",
        request,
        next_state="ReceiveFileHash",
        request_data=dict(index=index))

  def _ReceiveFetchedFile(self, tracker):
    """Remove pathspec for this index and call the ReceiveFetchedFile method."""
    index = tracker["index"]
    _, request_data = self.state.indexed_pathspecs[index]
    self.state.indexed_pathspecs[index] = (None, None)
    self.state.pending_hashes.pop(index, None)
    self.state.pending_files.pop(index, None)

    # Report the request_data for this flow's caller.
    self.ReceiveFetchedFile(
        tracker["stat_entry"], tracker["hash_obj"], request_data=request_data)

    # We have a bit more room in the pending_hashes so we try to schedule
    # another pathspec.
    self._TryToStartNextPathspec()

  def ReceiveFetchedFile(self, stat_entry, file_hash, request_data=None):
    """This method will be called for each new file successfully fetched.

    Args:
      stat_entry: rdf_client.StatEntry object describing the file.
      file_hash: rdf_crypto.Hash object with file hashes.
      request_data: Arbitrary dictionary that was passed to the corresponding
                    StartFileFetch call.
    """

  def _FileFetchFailed(self, index, request_name):
    """Remove pathspec for this index and call the FileFetchFailed method."""
    # Remove pathspec and request_data from index.
    pathspec, request_data = self.state.indexed_pathspecs[index]
    self.state.indexed_pathspecs[index] = (None, None)
    self.state.pending_hashes.pop(index, None)
    self.state.pending_files.pop(index, None)

    # Report the request_data for this flow's caller.
    self.FileFetchFailed(pathspec, request_name, request_data=request_data)

    # We have a bit more room in the pending_hashes so we try to schedule
    # another pathspec.
    self._TryToStartNextPathspec()

  def FileFetchFailed(self, pathspec, request_name, request_data=None):
    """This method will be called when stat or hash requests fail.

    Args:
      pathspec: Pathspec of a file that failed to be fetched.
      request_name: Name of a failed client action.
      request_data: Arbitrary dictionary that was passed to the corresponding
                    StartFileFetch call.
    """

  @flow.StateHandler()
  def StoreStat(self, responses):
    """Stores stat entry in the flow's state."""
    index = responses.request_data["index"]
    if not responses.success:
      self.Log("Failed to stat file: %s", responses.status)
      # Report failure.
      self._FileFetchFailed(index, responses.request.request.name)
      return

    tracker = self.state.pending_hashes[index]
    tracker["stat_entry"] = responses.First()

  @flow.StateHandler()
  def ReceiveFileHash(self, responses):
    """Add hash digest to tracker and check with filestore."""
    # Support old clients which may not have the new client action in place yet.
    # TODO(user): Deprecate once all clients have the HashFile action.
    if not responses.success and responses.request.request.name == "HashFile":
      logging.debug(
          "HashFile action not available, falling back to FingerprintFile.")
      self.CallClient(
          "FingerprintFile",
          responses.request.request.payload,
          next_state="ReceiveFileHash",
          request_data=responses.request_data)
      return

    index = responses.request_data["index"]
    if not responses.success:
      self.Log("Failed to hash file: %s", responses.status)
      self.state.pending_hashes.pop(index, None)
      # Report the error.
      self._FileFetchFailed(index, responses.request.request.name)
      return

    self.state.files_hashed += 1
    response = responses.First()
    if response.HasField("hash"):
      hash_obj = response.hash
    else:
      # Deprecate this method of returning hashes.
      hash_obj = rdf_crypto.Hash()

      if len(response.results) < 1 or response.results[0]["name"] != "generic":
        self.Log("Failed to hash file: %s",
                 self.state.indexed_pathspecs[index][0])
        self.state.pending_hashes.pop(index, None)
        return

      result = response.results[0]

      try:
        for hash_type in ["md5", "sha1", "sha256"]:
          value = result.GetItem(hash_type)
          setattr(hash_obj, hash_type, value)
      except AttributeError:
        self.Log("Failed to hash file: %s",
                 self.state.indexed_pathspecs[index][0])
        self.state.pending_hashes.pop(index, None)
        return

    try:
      tracker = self.state.pending_hashes[index]
    except KeyError:
      # Hashing the file failed, but we did stat it.
      self._FileFetchFailed(index, responses.request.request.name)
      return

    tracker["hash_obj"] = hash_obj
    tracker["bytes_read"] = response.bytes_read

    self.state.files_hashed_since_check += 1
    if self.state.files_hashed_since_check >= self.MIN_CALL_TO_FILE_STORE:
      self._CheckHashesWithFileStore()

  def _CheckHashesWithFileStore(self):
    """Check all queued up hashes for existence in file store.

    Hashes which do not exist in the file store will be downloaded. This
    function flushes the entire queue (self.state.pending_hashes) in order to
    minimize the round trips to the file store.

    If a file was found in the file store it is copied from there into the
    client's VFS namespace. Otherwise, we request the client to hash every block
    in the file, and add it to the file tracking queue
    (self.state.pending_files).
    """
    if not self.state.pending_hashes:
      return

    # This map represents all the hashes in the pending urns.
    file_hashes = {}

    # Store a mapping of hash to tracker. Keys are hashdigest objects,
    # values are arrays of tracker dicts.
    hash_to_tracker = {}
    for index, tracker in self.state.pending_hashes.iteritems():

      # We might not have gotten this hash yet
      if tracker.get("hash_obj") is None:
        continue

      hash_obj = tracker["hash_obj"]
      digest = hash_obj.sha256
      file_hashes[index] = hash_obj
      hash_to_tracker.setdefault(digest, []).append(tracker)

    # First we get all the files which are present in the file store.
    files_in_filestore = set()

    # TODO(user): This object never changes, could this be a class attribute?
    filestore_obj = aff4.FACTORY.Open(
        filestore.FileStore.PATH,
        filestore.FileStore,
        mode="r",
        token=self.token)

    for file_store_urn, hash_obj in filestore_obj.CheckHashes(
        file_hashes.values(), external=self.state.use_external_stores):

      self.HeartBeat()

      # Since checkhashes only returns one digest per unique hash we need to
      # find any other files pending download with the same hash.
      for tracker in hash_to_tracker[hash_obj.sha256]:
        self.state.files_skipped += 1
        file_hashes.pop(tracker["index"])
        files_in_filestore.add(file_store_urn)
        # Remove this tracker from the pending_hashes store since we no longer
        # need to process it.
        self.state.pending_hashes.pop(tracker["index"])

    # Now that the check is done, reset our counter
    self.state.files_hashed_since_check = 0

    # Now copy all existing files to the client aff4 space.
    for existing_blob in aff4.FACTORY.MultiOpen(
        files_in_filestore, mode="rw", token=self.token):

      hashset = existing_blob.Get(existing_blob.Schema.HASH)
      if hashset is None:
        self.Log("Filestore File %s has no hash.", existing_blob.urn)
        continue

      for file_tracker in hash_to_tracker.get(hashset.sha256, []):
        stat_entry = file_tracker["stat_entry"]
        # Due to potential filestore corruption, the existing_blob files can
        # have 0 size, make sure our size matches the actual size in that case.
        if existing_blob.size == 0:
          existing_blob.size = (file_tracker["bytes_read"] or
                                stat_entry.st_size)

        # Create a file in the client name space with the same classtype and
        # populate its attributes.
        stat_entry.aff4path = aff4_grr.VFSGRRClient.PathspecToURN(
            stat_entry.pathspec, self.client_id)

        with aff4.FACTORY.Create(
            stat_entry.aff4path,
            existing_blob.__class__,
            mode="w",
            token=self.token) as fd:

          fd.FromBlobImage(existing_blob)
          fd.Set(hashset)

        # Add this file to the index at the canonical location
        existing_blob.AddIndex(stat_entry.aff4path)

        # Report this hit to the flow's caller.
        self._ReceiveFetchedFile(file_tracker)

    # Now we iterate over all the files which are not in the store and arrange
    # for them to be copied.
    for index in file_hashes:

      # Move the tracker from the pending hashes store to the pending files
      # store - it will now be downloaded.
      file_tracker = self.state.pending_hashes.pop(index)
      self.state.pending_files[index] = file_tracker

      # If we already know how big the file is we use that, otherwise fall back
      # to the size reported by stat.
      if file_tracker["bytes_read"] > 0:
        file_tracker["size_to_download"] = file_tracker["bytes_read"]
      else:
        file_tracker["size_to_download"] = file_tracker["stat_entry"].st_size

      # We do not have the file here yet - we need to retrieve it.
      expected_number_of_hashes = (
          file_tracker["size_to_download"] / self.CHUNK_SIZE + 1)

      # We just hash ALL the chunks in the file now. NOTE: This maximizes client
      # VFS cache hit rate and is far more efficient than launching multiple
      # GetFile flows.
      self.state.files_to_fetch += 1

      for i in range(expected_number_of_hashes):
        if i == expected_number_of_hashes - 1:
          # The last chunk is short.
          length = file_tracker["size_to_download"] % self.CHUNK_SIZE
        else:
          length = self.CHUNK_SIZE
        self.CallClient(
            "HashBuffer",
            pathspec=file_tracker["stat_entry"].pathspec,
            offset=i * self.CHUNK_SIZE,
            length=length,
            next_state="CheckHash",
            request_data=dict(index=index))

    if self.state.files_hashed % 100 == 0:
      self.Log("Hashed %d files, skipped %s already stored.",
               self.state.files_hashed, self.state.files_skipped)

  @flow.StateHandler()
  def CheckHash(self, responses):
    """Adds the block hash to the file tracker responsible for this vfs URN."""
    index = responses.request_data["index"]

    if index not in self.state.pending_files:
      # This is a blobhash for a file we already failed to read and logged as
      # below, check here to avoid logging dups.
      return

    file_tracker = self.state.pending_files[index]

    hash_response = responses.First()
    if not responses.success or not hash_response:
      urn = aff4_grr.VFSGRRClient.PathspecToURN(
          file_tracker["stat_entry"].pathspec, self.client_id)
      self.Log("Failed to read %s: %s" % (urn, responses.status))
      self._FileFetchFailed(index, responses.request.request.name)
      return

    file_tracker.setdefault("hash_list", []).append(hash_response)

    self.state.blobs_we_need.add(hash_response.data.encode("hex"))

    if len(self.state.blobs_we_need) > self.MIN_CALL_TO_FILE_STORE:
      self.FetchFileContent()

  def FetchFileContent(self):
    """Fetch as much as the file's content as possible.

    This drains the pending_files store by checking which blobs we already have
    in the store and issuing calls to the client to receive outstanding blobs.
    """
    if not self.state.pending_files:
      return

    # Check if we have all the blobs in the blob AFF4 namespace..
    stats = aff4.FACTORY.Stat(self.state.blobs_we_need, token=self.token)
    blobs_we_have = set([x["urn"] for x in stats])
    self.state.blobs_we_need = set()

    # Now iterate over all the blobs and add them directly to the blob image.
    for index, file_tracker in self.state.pending_files.iteritems():
      for hash_response in file_tracker.get("hash_list", []):
        # Make sure we read the correct pathspec on the client.
        hash_response.pathspec = file_tracker["stat_entry"].pathspec

        digest = hash_response.data.encode("hex")
        if digest in blobs_we_have:
          # If we have the data we may call our state directly.
          self.CallState(
              [hash_response],
              next_state="WriteBuffer",
              request_data=dict(index=index))

        else:
          # We dont have this blob - ask the client to transmit it.
          self.CallClient(
              "TransferBuffer",
              hash_response,
              next_state="WriteBuffer",
              request_data=dict(index=index))

      # Clear the file tracker's hash list.
      file_tracker["hash_list"] = []

  @flow.StateHandler()
  def WriteBuffer(self, responses):
    """Write the hash received to the blob image."""

    # Note that hashes must arrive at this state in the correct order since they
    # are sent in the correct order (either via CallState or CallClient).
    index = responses.request_data["index"]
    if index not in self.state.pending_files:
      return

    # Failed to read the file - ignore it.
    if not responses.success:
      self._FileFetchFailed(index, responses.request.request.name)
      return

    response = responses.First()
    file_tracker = self.state.pending_files.get(index)
    if file_tracker:
      file_tracker.setdefault("blobs", []).append(
          (response.data, response.length))

      download_size = file_tracker["size_to_download"]
      if (response.length < self.CHUNK_SIZE or
          response.offset + response.length >= download_size):

        # Write the file to the data store.
        stat_entry = file_tracker["stat_entry"]
        stat_entry.aff4path = aff4_grr.VFSGRRClient.PathspecToURN(
            stat_entry.pathspec, self.client_id)
        with aff4.FACTORY.Create(
            stat_entry.aff4path,
            aff4_grr.VFSBlobImage,
            mode="w",
            token=self.token) as fd:

          fd.SetChunksize(self.CHUNK_SIZE)
          fd.Set(fd.Schema.STAT(stat_entry))
          fd.Set(fd.Schema.PATHSPEC(stat_entry.pathspec))
          fd.Set(fd.Schema.CONTENT_LAST(rdfvalue.RDFDatetime().Now()))

          for digest, length in file_tracker["blobs"]:
            fd.AddBlob(digest, length)

          # Save some space.
          del file_tracker["blobs"]

        # File done, remove from the store and close it.
        self._ReceiveFetchedFile(file_tracker)

        # Publish the new file event to cause the file to be added to the
        # filestore. This is not time critical so do it when we have spare
        # capacity.
        self.Publish(
            "FileStore.AddFileToStore",
            stat_entry.aff4path,
            priority=rdf_flows.GrrMessage.Priority.LOW_PRIORITY)

        self.state.files_fetched += 1

        if not self.state.files_fetched % 100:
          self.Log("Fetched %d of %d files.", self.state.files_fetched,
                   self.state.files_to_fetch)

  @flow.StateHandler()
  def End(self):
    # There are some files still in flight.
    if self.state.pending_hashes or self.state.pending_files:
      self._CheckHashesWithFileStore()
      self.FetchFileContent()

    if not self.runner.OutstandingRequests():
      super(MultiGetFileMixin, self).End()


class MultiGetFileArgs(rdf_structs.RDFProtoStruct):
  protobuf = flows_pb2.MultiGetFileArgs


class MultiGetFile(MultiGetFileMixin, flow.GRRFlow):
  """A flow to effectively retrieve a number of files."""

  args_type = MultiGetFileArgs

  @flow.StateHandler()
  def Start(self):
    """Start state of the flow."""
    super(MultiGetFile, self).Start(
        file_size=self.args.file_size,
        maximum_pending_files=self.args.maximum_pending_files,
        use_external_stores=self.args.use_external_stores)

    unique_paths = set()

    for pathspec in self.args.pathspecs:

      vfs_urn = aff4_grr.VFSGRRClient.PathspecToURN(pathspec, self.client_id)

      if vfs_urn not in unique_paths:
        # Only Stat/Hash each path once, input pathspecs can have dups.
        unique_paths.add(vfs_urn)

        self.StartFileFetch(pathspec)

  def ReceiveFetchedFile(self, stat_entry, unused_hash_obj, request_data=None):
    """This method will be called for each new file successfully fetched."""
    _ = request_data
    self.SendReply(stat_entry)


class FileStoreCreateFile(flow.EventListener):
  """Receive an event about a new file and add it to the file store.

  The file store is a central place where files are managed in the data
  store. Files are deduplicated and stored centrally.

  This event listener will be fired when a new file is downloaded through
  e.g. the GetFile flow. We then recalculate the file's hashes and store it in
  the data store under a canonical URN.
  """

  EVENTS = ["FileStore.AddFileToStore"]

  well_known_session_id = rdfvalue.SessionID(flow_name="FileStoreCreateFile")

  CHUNK_SIZE = 512 * 1024

  @flow.EventHandler()
  def ProcessMessage(self, message=None, event=None):
    """Process the new file and add to the file store."""
    _ = event
    vfs_urn = message.payload

    vfs_fd = aff4.FACTORY.Open(vfs_urn, mode="rw", token=self.token)
    filestore_fd = aff4.FACTORY.Create(
        filestore.FileStore.PATH,
        filestore.FileStore,
        mode="w",
        token=self.token)
    filestore_fd.AddFile(vfs_fd)
    vfs_fd.Flush(sync=False)


class GetMBRArgs(rdf_structs.RDFProtoStruct):
  protobuf = flows_pb2.GetMBRArgs


class GetMBR(flow.GRRFlow):
  """A flow to retrieve the MBR.

  Returns to parent flow:
    The retrieved MBR.
  """

  category = "/Filesystem/"
  args_type = GetMBRArgs
  behaviours = flow.GRRFlow.behaviours + "BASIC"

  @flow.StateHandler()
  def Start(self):
    """Schedules the ReadBuffer client action."""
    pathspec = rdf_paths.PathSpec(
        path="\\\\.\\PhysicalDrive0\\",
        pathtype=rdf_paths.PathSpec.PathType.OS,
        path_options=rdf_paths.PathSpec.Options.CASE_LITERAL)

    request = rdf_client.BufferReference(
        pathspec=pathspec, offset=0, length=self.args.length)
    self.CallClient("ReadBuffer", request, next_state="StoreMBR")

  @flow.StateHandler()
  def StoreMBR(self, responses):
    """This method stores the MBR."""

    if not responses.success:
      msg = "Could not retrieve MBR: %s" % responses.status
      self.Log(msg)
      raise flow.FlowError(msg)

    response = responses.First()

    mbr = aff4.FACTORY.Create(
        self.client_id.Add("mbr"), aff4_grr.VFSFile, mode="w", token=self.token)
    mbr.write(response.data)
    mbr.Close()
    self.Log("Successfully stored the MBR (%d bytes)." % len(response.data))
    self.SendReply(rdfvalue.RDFBytes(response.data))


class TransferStore(flow.WellKnownFlow):
  """Store a buffer into a determined location."""
  well_known_session_id = rdfvalue.SessionID(flow_name="TransferStore")

  def ProcessMessages(self, msg_list):
    blobs = []
    for message in msg_list:
      if (message.auth_state !=
          rdf_flows.GrrMessage.AuthorizationState.AUTHENTICATED):
        logging.error("TransferStore request from %s is not authenticated.",
                      message.source)
        continue

      read_buffer = message.payload
      data = read_buffer.data
      if not data:
        continue

      if (read_buffer.compression ==
          rdf_protodict.DataBlob.CompressionType.ZCOMPRESSION):
        data = zlib.decompress(data)
      elif (read_buffer.compression ==
            rdf_protodict.DataBlob.CompressionType.UNCOMPRESSED):
        pass
      else:
        raise RuntimeError("Unsupported compression")

      blobs.append(data)

    data_store.DB.StoreBlobs(blobs, token=self.token)

  def ProcessMessage(self, message):
    """Write the blob into the AFF4 blob storage area."""
    return self.ProcessMessages([message])


class SendFile(flow.GRRFlow):
  """This flow sends a file to remote listener.

  To use this flow, choose a key and an IV in hex format (if run from the GUI,
  there will be a pregenerated pair key and iv for you to use) and run a
  listener on the server you want to use like this:

  nc -l <port> | openssl aes-128-cbc -d -K <key> -iv <iv> > <filename>

  Returns to parent flow:
    A rdf_client.StatEntry of the sent file.
  """

  category = "/Filesystem/"
  args_type = rdf_client.SendFileRequest

  @flow.StateHandler()
  def Start(self):
    """This issues the sendfile request."""
    self.CallClient("SendFile", self.args, next_state="Done")

  @flow.StateHandler()
  def Done(self, responses):
    if not responses.success:
      self.Log(responses.status.error_message)
      raise flow.FlowError(responses.status.error_message)


class LoadComponentMixin(object):
  """A mixin which loads components on the client.

  Use this mixin to force the client to load the required components prior to
  launching client actions implemented by those components.
  """

  # We handle client exits by ourselves.
  handles_crashes = True

  def LoadComponentOnClient(self, name=None, version=None, next_state=None):
    """Load the component with the specified name and version."""
    if next_state is None:
      raise TypeError("next_state not specified.")

    client = aff4.FACTORY.Open(self.client_id, token=self.token)
    system = unicode(client.Get(client.Schema.SYSTEM) or "").lower()

    # TODO(user): Remove python hack when client 3.1 is pushed.
    request_data = dict(name=name, version=version, next_state=next_state)
    python_hack_root_urn = config_lib.CONFIG.Get("Config.python_hack_root")
    python_hack_path = python_hack_root_urn.Add(system).Add(
        "restart_if_component_loaded.py")

    fd = aff4.FACTORY.Open(python_hack_path, token=self.token)
    if not isinstance(fd, collects.GRRSignedBlob):
      logging.info("Python hack %s not available.", python_hack_path)

      self.CallStateInline(
          next_state="LoadComponentAfterFlushOldComponent",
          request_data=request_data)
    else:
      logging.info("Sending python hack %s", python_hack_path)

      for python_blob in fd:
        self.CallClient(
            "ExecutePython",
            python_code=python_blob,
            py_args=dict(
                name=name, version=version),
            next_state="LoadComponentAfterFlushOldComponent",
            request_data=request_data)

  @flow.StateHandler()
  def LoadComponentAfterFlushOldComponent(self, responses):
    """Load the component."""
    request_data = responses.request_data
    name = request_data["name"]
    version = request_data["version"]
    next_state = request_data["next_state"]

    # Get the component summary.
    component_urn = config_lib.CONFIG.Get("Config.aff4_root").Add(
        "components").Add("%s_%s" % (name, version))

    try:
      fd = aff4.FACTORY.Open(
          component_urn,
          aff4_type=collects.ComponentObject,
          mode="r",
          token=self.token)
    except IOError as e:
      raise IOError("Required component not found: %s" % e)

    component_summary = fd.Get(fd.Schema.COMPONENT)
    if component_summary is None:
      raise RuntimeError("Component %s (%s) does not exist in data store." %
                         (name, version))

    self.CallClient(
        "LoadComponent",
        summary=component_summary,
        next_state="ComponentLoaded",
        request_data=dict(next_state=next_state))

  @flow.StateHandler()
  def ComponentLoaded(self, responses):
    if not responses.success:
      self.Log(responses.status.error_message)
      raise flow.FlowError(responses.status.error_message)

    self.Log("Loaded component %s", responses.First().summary.name)
    self.CallStateInline(next_state=responses.request_data["next_state"])
