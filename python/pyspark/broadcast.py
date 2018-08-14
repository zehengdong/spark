#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import gc
import os
import sys
from tempfile import NamedTemporaryFile

from pyspark.java_gateway import local_connect_and_auth
from pyspark.serializers import ChunkedStream

if sys.version < '3':
    import cPickle as pickle
else:
    import pickle
    unicode = str

__all__ = ['Broadcast']


# Holds broadcasted data received from Java, keyed by its id.
_broadcastRegistry = {}


def _from_id(bid):
    from pyspark.broadcast import _broadcastRegistry
    if bid not in _broadcastRegistry:
        raise Exception("Broadcast variable '%s' not loaded!" % bid)
    return _broadcastRegistry[bid]


class Broadcast(object):

    """
    A broadcast variable created with L{SparkContext.broadcast()}.
    Access its value through C{.value}.

    Examples:

    >>> from pyspark.context import SparkContext
    >>> sc = SparkContext('local', 'test')
    >>> b = sc.broadcast([1, 2, 3, 4, 5])
    >>> b.value
    [1, 2, 3, 4, 5]
    >>> sc.parallelize([0, 0]).flatMap(lambda x: b.value).collect()
    [1, 2, 3, 4, 5, 1, 2, 3, 4, 5]
    >>> b.unpersist()

    >>> large_broadcast = sc.broadcast(range(10000))
    """

    def __init__(self, sc=None, value=None, pickle_registry=None, path=None,
                 sock_file=None):
        """
        Should not be called directly by users -- use L{SparkContext.broadcast()}
        instead.
        """
        if sc is not None:
            # we're on the driver.  We want the pickled data to end up in a file (maybe encrypted)
            f = NamedTemporaryFile(delete=False, dir=sc._temp_dir)
            self._path = f.name
            python_broadcast = sc._jvm.PythonRDD.setupBroadcast(self._path)
            if sc._encryption_enabled:
                # with encryption, we ask the jvm to do the encryption for us, we send it data
                # over a socket
                port, auth_secret = python_broadcast.setupEncryptionServer()
                (encryption_sock_file, _) = local_connect_and_auth(port, auth_secret)
                broadcast_out = ChunkedStream(encryption_sock_file, 8192)
            else:
                # no encryption, we can just write pickled data directly to the file from python
                broadcast_out = f
            self.dump(value, broadcast_out)
            if sc._encryption_enabled:
                python_broadcast.waitTillDataReceived()
            self._jbroadcast = sc._jsc.broadcast(python_broadcast)
            self._pickle_registry = pickle_registry
        else:
            # we're on an executor
            self._jbroadcast = None
            if sock_file is not None:
                # the jvm is doing decryption for us.  Read the value
                # immediately from the sock_file
                self._value = self.load(sock_file)
            else:
                # the jvm just dumps the pickled data in path -- we'll unpickle lazily when
                # the value is requested
                assert(path is not None)
                self._path = path

    def dump(self, value, f):
        pickle.dump(value, f, 2)
        f.close()

    def load_from_path(self, path):
        with open(path, 'rb', 1 << 20) as f:
            return self.load(f)

    def load(self, file):
        # "file" could also be a socket
        gc.disable()
        try:
            return pickle.load(file)
        finally:
            gc.enable()

    @property
    def value(self):
        """ Return the broadcasted value
        """
        if not hasattr(self, "_value") and self._path is not None:
            self._value = self.load_from_path(self._path)
        return self._value

    def unpersist(self, blocking=False):
        """
        Delete cached copies of this broadcast on the executors.
        """
        if self._jbroadcast is None:
            raise Exception("Broadcast can only be unpersisted in driver")
        self._jbroadcast.unpersist(blocking)
        os.unlink(self._path)

    def __reduce__(self):
        if self._jbroadcast is None:
            raise Exception("Broadcast can only be serialized in driver")
        self._pickle_registry.add(self)
        return _from_id, (self._jbroadcast.id(),)


if __name__ == "__main__":
    import doctest
    (failure_count, test_count) = doctest.testmod()
    if failure_count:
        exit(-1)
