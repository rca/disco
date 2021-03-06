import sys, re, os, marshal, urllib, httplib, cjson, time, cPickle
from disco import func, util
from netstring import *


class JobException(Exception):
        def __init__(self, msg, master, name):
                self.msg = msg
                self.name = name
                self.master = master

        def __str__(self):
                return "Job %s/%s failed: %s" %\
                        (self.master, self.name, self.msg)


class Params:
        def __init__(self, **kwargs):
                self._state = {}
                for k, v in kwargs.iteritems():
                        setattr(self, k, v)

        def __setattr__(self, k, v):
                if k[0] == '_':
                        self.__dict__[k] = v
                        return
                st_v = v
                st_k = "n_" + k
                try:
                        st_v = marshal.dumps(v.func_code)
                        st_k = "f_" + k
                except AttributeError:
                        pass
                self._state[st_k] = st_v
                self.__dict__[k] = v
        
        def __getstate__(self):
                return self._state

        def __setstate__(self, state):
                self._state = {}
                for k, v in state.iteritems():
                        if k.startswith('f_'):
                                t = lambda x: x
                                t.func_code = marshal.loads(v)
                                v = t
                        self.__dict__[k[2:]] = v

class Stats(object):
        def __init__(self, prof_data):
                self.stats = marshal.loads(prof_data)
        def create_stats(self):
                pass

class Disco(object):

        def __init__(self, host):
                self.host = util.disco_host(host)[7:]
                self.conn = httplib.HTTPConnection(self.host)

        def request(self, url, data = None, raw_handle = False):
                try:
                        if data:
                                self.conn.request("POST", url, data)
                        else:
                                self.conn.request("GET", url, None)
                        r = self.conn.getresponse()
                        if raw_handle:
                                return r
                        else:
                                return r.read()
                except httplib.BadStatusLine:
                        self.conn.close()
                        self.conn = httplib.HTTPConnection(self.host)
                        return self.request(url, data)
        
        def nodeinfo(self):
                return cjson.decode(self.request("/disco/ctrl/nodeinfo"))

        def joblist(self):
                return cjson.decode(self.request("/disco/ctrl/joblist"))
        
        def oob_get(self, name, key):
                r = urllib.urlopen(\
                        "http://%s/disco/ctrl/oob_get?name=%s&key=%s" %\
                                (self.host, name, key))
                if "status" in r.headers and\
                        not r.headers["status"].startswith("200"):
                        raise JobException("Unknown job or key",\
                                self.host, name)
                return r.read()

        def oob_list(self, name):
                r = urllib.urlopen(\
                        "http://%s/disco/ctrl/oob_list?name=%s" %\
                                (self.host, name))
                if "status" in r.headers and\
                        not r.headers["status"].startswith("200"):
                        raise JobException("Unknown job", self.host, name)
                return cjson.decode(r.read())

        def profile_stats(self, name, mode = ""):
                import pstats
                if mode:
                        prefix = "profile-%s-" % mode
                else:
                        prefix = "profile-"
                f = [s for s in self.oob_list(name) if s.startswith(prefix)]
                if not f:
                        raise JobException("No profile data", self.host, name)
                
                stats = pstats.Stats(Stats(self.oob_get(name, f[0])))
                for s in f[1:]:
                        stats.add(Stats(self.oob_get(name, s)))
                return stats
        
        def new_job(self, **kwargs):
                return Job(self, **kwargs)
        
        def kill(self, name):
                self.request("/disco/ctrl/kill_job", '"%s"' % name)
        
        def clean(self, name):
                self.request("/disco/ctrl/clean_job", '"%s"' % name)

        def purge(self, name):
                self.request("/disco/ctrl/purge_job", '"%s"' % name)

        def jobspec(self, name):
                # Parameters request is handled with a separate connection that
                # knows how to handle redirects.
                r = urllib.urlopen("http://%s/disco/ctrl/parameters?name=%s"\
                        % (self.host, name))
                return decode_netstring_fd(r)

        def results(self, name):
                r = self.request("/disco/ctrl/get_results?name=" + name)
                if r:
                        return cjson.decode(r)
                else:
                        return None

        def jobinfo(self, name):
                r = self.request("/disco/ctrl/jobinfo?name=" + name)
                if r:
                        return cjson.decode(r)
                else:
                        return r

        def wait(self, name, poll_interval = 5, timeout = None, clean = False):
                t = time.time()
                while True:
                        time.sleep(poll_interval)
                        status = self.results(name)
                        if status == None:
                                raise JobException("Unknown job", self.host, name)
                        if status[0] == "ready":
                                if clean:
                                        self.clean(name)
                                return status[1]
                        if status[0] != "active":
                                raise JobException("Job failed", self.host, name)
                        if timeout and time.time() - t > timeout:
                                raise JobException("Timeout", self.host, name)


class Job(object):

        defaults = {"name": None,
                    "map": None,
                    "input": None,
                    "map_init": None,
                    "reduce_init": None,
                    "map_reader": func.map_line_reader,
                    "map_writer": func.netstr_writer,
                    "reduce_reader": func.netstr_reader,
                    "reduce_writer": func.netstr_writer,
                    "reduce": None,
                    "partition": func.default_partition,
                    "combiner": None,
                    "nr_maps": None,
                    "nr_reduces": None,
                    "sort": False,
                    "params": Params(),
                    "mem_sort_limit": 256 * 1024**2,
                    "chunked": None,
                    "ext_params": None,
                    "status_interval": 100000,
                    "required_modules": [],
                    "profile": False}

        def __init__(self, master, **kwargs):
                self.master = master
                if "name" not in kwargs:
                        raise Exception("Argument name is required")
                if re.search("\W", kwargs["name"]):
                        raise Exception("Only characters in [a-zA-Z0-9_] "\
                              "are allowed in the job name")
                self.name = "%s@%d" % (kwargs["name"], int(time.time()))
                self._run(**kwargs)

        def __getattr__(self, name):
                def r(f):
                        def g(*args, **kw):
                                return f(*tuple([self.name] + list(args)), **kw)
                        return g
                if name in ["kill", "clean", "purge", "jobspec", "results",
                            "jobinfo", "wait", "oob_get", "oob_list",
                            "profile_stats"]:
                        return r(getattr(self.master, name))
                raise AttributeError("%s not found" % name)
       
        def _run(self, **kw):
                d = lambda x: kw.get(x, Job.defaults[x])

                # Backwards compatibility 
                # (fun_map == map, input_files == input)
                if "fun_map" in kw:
                        kw["map"] = kw["fun_map"]
                
                if "input_files" in kw:
                        kw["input"] = kw["input_files"]
                
                if not "input" in kw:
                        raise Exception("input is required")
                
                if not ("map" in kw or "reduce" in kw):
                        raise Exception("Specify map and/or reduce")
                
                for p in kw:
                        if p not in Job.defaults:
                                raise Exception("Unknown argument: %s" % p)

                inputs = kw["input"]
                
                req = {"name": self.name,
                       "version": ".".join(map(str, sys.version_info[:2])),
                       "params": cPickle.dumps(d("params")),
                       "sort": str(int(d("sort"))),
                       "mem_sort_limit": str(d("mem_sort_limit")),
                       "status_interval": str(d("status_interval")),
                       "required_modules": " ".join(d("required_modules")),
                       "profile": str(int(d("profile")))}

                if "map" in kw:
                        if type(kw["map"]) == dict:
                                req["ext_map"] = marshal.dumps(kw["map"])
                        else:
                                req["map"] = marshal.dumps(kw["map"].func_code)

                        if "nr_maps" not in kw or kw["nr_maps"] > len(inputs):
                                nr_maps = len(inputs)
                        else:
                                nr_maps = kw["nr_maps"]

                        if "map_init" in kw:
                                req["map_init"] = marshal.dumps(\
                                        kw["map_init"].func_code)
                       
                        req["map_reader"] =\
                                marshal.dumps(d("map_reader").func_code)
                        req["map_writer"] =\
                                marshal.dumps(d("map_writer").func_code)
                        req["partition"] =\
                                marshal.dumps(d("partition").func_code)
                        
                        parsed_inputs = []
                        for inp in inputs:
                                if inp.startswith("dir://"):
                                        parsed_inputs += util.parse_dir(inp)
                                else:
                                        parsed_inputs.append(inp)
                        inputs = parsed_inputs
                else:
                        addr = [x for x in inputs\
                                if not x.startswith("dir://")]

                        if d("nr_reduces") == None and not addr:
                                raise Exception("nr_reduces must match to "\
                                        "the number of partitions in the "\
                                        "input data")

                        if d("nr_reduces") != 1 and addr: 
                                raise Exception("nr_reduces must be 1 when "\
                                        "using external inputs without "\
                                        "the map phase")
                        nr_maps = 0
               
                req["input"] = " ".join(inputs)
                req["nr_maps"] = str(nr_maps)
        
                if "ext_params" in kw:
                        if type(kw["ext_params"]) == dict:
                                req["ext_params"] =\
                                        encode_netstring_fd(kw["ext_params"])
                        else:
                                req["ext_params"] = kw["ext_params"]
        
                nr_reduces = d("nr_reduces")
                if "reduce" in kw:
                        if type(kw["reduce"]) == dict:
                                req["ext_reduce"] = marshal.dumps(kw["reduce"])
                                req["reduce"] = ""
                        else:
                                req["reduce"] = marshal.dumps(
                                        kw["reduce"].func_code)
                        nr_reduces = nr_reduces or max(nr_maps / 2, 1)
                        req["chunked"] = "True"
                       
                        req["reduce_reader"] =\
                                marshal.dumps(d("reduce_reader").func_code)
                        req["reduce_writer"] =\
                                marshal.dumps(d("reduce_writer").func_code)

                        if "reduce_init" in kw:
                                req["reduce_init"] = marshal.dumps(\
                                        kw["reduce_init"].func_code)
                else:
                        nr_reduces = nr_reduces or 1
                
                req["nr_reduces"] = str(nr_reduces)

                if d("chunked") != None:
                        if d("chunked"):
                                req["chunked"] = "True"
                        elif "chunked" in req:
                                del req["chunked"]

                if "combiner" in kw:
                        req["combiner"] =\
                                marshal.dumps(kw["combiner"].func_code)

                self.msg = encode_netstring_fd(req)
                reply = self.master.request("/disco/job/new", self.msg)
                        
                if reply != "job started":
                        raise Exception("Failed to start a job. Server replied: " + reply)



def result_iterator(results, notifier = None,\
        proxy = None, reader = func.netstr_reader):
        
        if not proxy:
                proxy = os.environ.get("DISCO_PROXY", None)
        if proxy:
                if proxy.startswith("disco://"):
                        proxy = "%s:%s" % (proxy[8:], util.MASTER_PORT)
                elif proxy.startswith("http://"):
                        proxy = proxy[7:]
        res = []
        for dir_url in results:
                if dir_url.startswith("dir://"):
                        res += util.parse_dir(dir_url, proxy)
                else:
                        res.append(dir_url)
        
        for url in res:
                if url.startswith("file://"):
                        fname = url[7:]
                        fd = file(fname)
                        sze = os.stat(fname).st_size
                        http = None
                else:
                        host, fname = url[8:].split("/", 1)
                        if proxy:
                                ext_host = proxy
                                fname = "/disco/node/%s/%s" % (host, fname)
                        else:
                                ext_host = host + ":" + util.HTTP_PORT
                        ext_file = "/" + fname

                        http = httplib.HTTPConnection(ext_host)
                        http.request("GET", ext_file, "")
                        fd = http.getresponse()
                        if fd.status != 200:
                                raise "HTTP error %d" % fd.status
                
                        sze = int(fd.getheader("content-length"))

                if notifier:
                        notifier(url)

                for x in reader(fd, sze, fname):
                        yield x
                
                if http:
                        http.close()
                else:
                        fd.close()

