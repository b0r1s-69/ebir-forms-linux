/* eBIRForms native compatibility layer.
 *
 * The BIR forms are HTA/JScript documents written for the Windows Script Host
 * automation objects.  This file re-implements those objects on top of the
 * native host RPC (see server.py) so the unmodified BIR scripts run in
 * QtWebEngine/Chromium on Linux.
 *
 * Injected at document creation into the main world, before any page script.
 */
(function () {
  'use strict';
  if (window.__ebirCompat) { return; }
  window.__ebirCompat = { version: '1.0.0' };

  /* ------------------------------------------------------------------ rpc */
  function rpc(op, payload) {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/__host/' + op, false);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.send(JSON.stringify(payload === undefined ? {} : payload));
    if (xhr.status !== 200) {
      throw new Error('host rpc ' + op + ' failed with HTTP ' + xhr.status + ': ' + xhr.responseText);
    }
    var out = JSON.parse(xhr.responseText);
    if (out && out.ok === false && out.error) {
      var err = new Error(out.error);
      err.rpcError = out;
      throw err;
    }
    return out;
  }
  window.__ebirRPC = rpc;

  function hostLog(level, msg) {
    try { rpc('host/log', { level: level, msg: String(msg) }); } catch (e) { /* ignore */ }
  }
  window.__ebirHostLog = hostLog;

  /* ------------------------------------------------------- path utilities */
  function pathSplit(p) {
    return String(p === undefined || p === null ? '' : p).replace(/\\/g, '/').split('/');
  }
  function getFileName(p) {
    var parts = pathSplit(p);
    for (var i = parts.length - 1; i >= 0; i--) { if (parts[i]) { return parts[i]; } }
    return '';
  }
  function getParentFolderName(p) {
    var s = String(p === undefined || p === null ? '' : p).replace(/\\/g, '/');
    s = s.replace(/\/+$/, '');
    var idx = s.lastIndexOf('/');
    return idx <= 0 ? s.slice(0, Math.max(idx, 0)) : s.slice(0, idx);
  }
  function getBaseName(p) {
    var name = getFileName(p);
    var dot = name.lastIndexOf('.');
    return dot > 0 ? name.slice(0, dot) : name;
  }
  function getExtensionName(p) {
    var name = getFileName(p);
    var dot = name.lastIndexOf('.');
    return dot > 0 ? name.slice(dot + 1) : '';
  }

  /* ------------------------------------------------------- case folding */
  // COM automation objects expose properties case-insensitively ("Name" and
  // "name" both work); plain JavaScript objects do not. The BIR scripts rely on
  // that (e.g. FSo.item().name), so every object this layer hands back is
  // wrapped in a proxy that folds property names the way IDispatch did.
  function ci(obj) {
    if (obj === null || typeof obj !== 'object' || obj.__ebirCI) { return obj; }
    var fold = function (target, prop) {
      if (typeof prop !== 'string') { return undefined; }
      var lowered = prop.toLowerCase();
      for (var key in target) {
        if (key.toLowerCase() === lowered) { return key; }
      }
      return undefined;
    };
    var proxy = new Proxy(obj, {
      get: function (target, prop) {
        if (typeof prop !== 'string') { return Reflect.get(target, prop); }
        if (prop in target) { return Reflect.get(target, prop); }
        var actual = fold(target, prop);
        return actual === undefined ? undefined : Reflect.get(target, actual);
      },
      set: function (target, prop, value) {
        if (typeof prop === 'string' && !(prop in target)) {
          var actual = fold(target, prop);
          if (actual !== undefined) { target[actual] = value; return true; }
        }
        return Reflect.set(target, prop, value);
      },
      has: function (target, prop) {
        if (typeof prop === 'string' && !(prop in target)) { return fold(target, prop) !== undefined; }
        return Reflect.has(target, prop);
      }
    });
    Object.defineProperty(obj, '__ebirCI', { value: true, enumerable: false });
    return proxy;
  }
  window.__ebirCI = ci;

  /* ---------------------------------------------------------- collections */
  function makeCollection(items) {
    var coll = {
      Count: items.length,
      length: items.length,
      Item: function (key) {
        if (typeof key === 'number') { return items[key]; }
        var wanted = String(key).toLowerCase();
        for (var i = 0; i < items.length; i++) {
          if (String(items[i].Name).toLowerCase() === wanted) { return items[i]; }
        }
        return null;
      },
      _items: items
    };
    items.forEach(function (item, index) { coll[index] = item; });
    return ci(coll);
  }

  /* -------------------------------------------------------------- streams */
  var FOR_READING = 1, FOR_WRITING = 2, FOR_APPENDING = 8;
  var TRISTATE_DEFAULT = -2, TRITATE_ASCII = 0, TRISTATE_UNICODE = -1;

  function TextStream(spec) {
    this.Path = spec.path;
    this.__spec = spec;
    this.__text = spec.text || '';
    this.__pos = 0;
    this.__line = 1;
    this.__closed = false;
    this.__dirty = false;
    this.__encoding = spec.encoding || (spec.unicode ? 'utf-16-le' : 'utf-8');
    this.__append = !!spec.append;
    return ci(this);
  }
  TextStream.prototype.__ensureOpen = function () {
    if (this.__closed) { throw new Error('The file is closed'); }
  };
  TextStream.prototype.__flush = function () {
    if (!this.__dirty) { return; }
    rpc('fs/write', {
      path: this.__spec.path,
      text: this.__text,
      encoding: this.__encoding,
      append: this.__append
    });
    this.__dirty = false;
    this.__append = true; // subsequent flushes are appends of the full buffer
  };
  Object.defineProperty(TextStream.prototype, 'AtEndOfStream', {
    get: function () { return this.__pos >= this.__text.length; }
  });
  Object.defineProperty(TextStream.prototype, 'AtEndOfLine', {
    get: function () {
      var ch = this.__text.charAt(this.__pos);
      return ch === '\r' || ch === '\n' || this.__pos >= this.__text.length;
    }
  });
  Object.defineProperty(TextStream.prototype, 'Line', { get: function () { return this.__line; } });
  Object.defineProperty(TextStream.prototype, 'Column', { get: function () { return this.__pos + 1; } });
  TextStream.prototype.Read = function (n) {
    this.__ensureOpen();
    var out = this.__text.substr(this.__pos, n);
    this.__pos += out.length;
    return out;
  };
  TextStream.prototype.ReadAll = function () {
    this.__ensureOpen();
    var out = this.__text.substr(this.__pos);
    this.__pos = this.__text.length;
    return out;
  };
  TextStream.prototype.ReadLine = function () {
    this.__ensureOpen();
    if (this.__pos >= this.__text.length) { return ''; }
    var rest = this.__text.slice(this.__pos);
    var m = rest.match(/^[^\r\n]*/);
    var line = m ? m[0] : '';
    this.__pos += line.length;
    if (this.__text.substr(this.__pos, 2) === '\r\n') { this.__pos += 2; }
    else if (this.__pos < this.__text.length) { this.__pos += 1; }
    this.__line += 1;
    return line;
  };
  TextStream.prototype.Skip = function (n) {
    this.__ensureOpen();
    this.__pos = Math.min(this.__pos + n, this.__text.length);
  };
  TextStream.prototype.SkipLine = function () { this.ReadLine(); };
  TextStream.prototype.Write = function (text) {
    this.__ensureOpen();
    if (this.__spec.mode === FOR_READING) { throw new Error('Stream not open for writing'); }
    this.__text += String(text === undefined ? '' : text);
    this.__dirty = true;
    this.__flush();
  };
  TextStream.prototype.WriteLine = function (text) {
    this.Write((text === undefined || text === null ? '' : String(text)) + '\r\n');
  };
  TextStream.prototype.WriteBlankLines = function (n) {
    for (var i = 0; i < n; i++) { this.WriteLine(''); }
  };
  TextStream.prototype.Close = function () {
    if (this.__closed) { return; }
    this.__flush();
    this.__closed = true;
  };

  /* -------------------------------------------------- File / Folder objects */
  function FileObject(path, stat) {
    stat = stat || {};
    this.Path = path;
    this.ShortPath = path;
    this.Name = stat.name || getFileName(path);
    this.Size = stat.size || 0;
    this.DateCreated = new Date((stat.mtime || 0) * 1000);
    this.DateLastModified = new Date((stat.mtime || 0) * 1000);
    this.DateLastAccessed = new Date((stat.mtime || 0) * 1000);
    this.Type = getExtensionName(path) ? getExtensionName(path).toUpperCase() + ' File' : 'File';
    this.Attributes = 32;
    this.ParentFolder = { Path: getParentFolderName(path) };
    return ci(this);
  }
  FileObject.prototype.OpenAsTextStream = function (iomode) {
    return fso.OpenTextFile(this.Path, iomode || FOR_READING);
  };
  FileObject.prototype.Delete = function () { fso.DeleteFile(this.Path, true); };
  FileObject.prototype.Copy = function (dst, overwrite) { fso.CopyFile(this.Path, dst, overwrite); };
  FileObject.prototype.Move = function (dst) { fso.MoveFile(this.Path, dst); };

  function FolderObject(path, entries) {
    entries = entries || [];
    this.Path = path;
    this.ShortPath = path;
    this.Name = getFileName(path);
    this.DateCreated = new Date();
    this.DateLastModified = new Date();
    this.Type = 'File folder';
    this.Attributes = 16;
    var files = [], dirs = [];
    entries.forEach(function (entry) {
      if (entry.is_dir) { dirs.push(new FolderObject(entry.abs || entry.path, [])); }
      else { files.push(new FileObject(entry.abs || entry.path, entry)); }
    });
    this.Files = makeCollection(files);
    this.SubFolders = makeCollection(dirs);
    this.ParentFolder = { Path: getParentFolderName(path) };
    return ci(this);
  }
  FolderObject.prototype.CreateTextFile = function (name, overwrite, unicode) {
    return fso.CreateTextFile(this.Path.replace(/\/+$/, '') + '/' + name, overwrite, unicode);
  };
  FolderObject.prototype.CreateFolder = function (name) {
    return fso.CreateFolder(this.Path.replace(/\/+$/, '') + '/' + name);
  };
  FolderObject.prototype.Delete = function (force) { fso.DeleteFolder(this.Path, force); };
  FolderObject.prototype.Copy = function (dst, overwrite) { fso.CopyFolder(this.Path, dst, overwrite); };
  FolderObject.prototype.Move = function (dst) { fso.MoveFolder(this.Path, dst); };

  /* ------------------------------------------------ FileSystemObject (FSO) */
  function FileSystemObject() {}
  var fso = ci(new FileSystemObject());

  fso.FileExists = function (path) {
    try { var s = rpc('fs/stat', { path: path }); return !!(s.exists && !s.is_dir); }
    catch (e) { return false; }
  };
  fso.FolderExists = function (path) {
    try { var s = rpc('fs/stat', { path: path }); return !!(s.exists && s.is_dir); }
    catch (e) { return false; }
  };
  fso.GetFile = function (path) {
    var s = rpc('fs/stat', { path: path });
    if (!s.exists) { throw new Error('Path not found: ' + path); }
    return new FileObject(s.abs, s);
  };
  fso.GetFolder = function (path) {
    var listing = rpc('fs/list', { path: path });
    if (!listing.exists) { throw new Error('Path not found: ' + path); }
    return new FolderObject(listing.abs, listing.entries);
  };
  fso.CreateFolder = function (path) {
    var r = rpc('fs/mkdir', { path: path });
    return new FolderObject(r.abs, []);
  };
  fso.DeleteFolder = function (path, force) { rpc('fs/delete', { path: path, recurse: force !== false }); };
  fso.CopyFolder = function (src, dst, overwrite) { rpc('fs/copy', { src: src, dst: dst, overwrite: overwrite !== false }); };
  fso.MoveFolder = function (src, dst) { rpc('fs/move', { src: src, dst: dst }); };
  fso.DeleteFile = function (path, force) { rpc('fs/delete', { path: path }); };
  fso.CopyFile = function (src, dst, overwrite) { rpc('fs/copy', { src: src, dst: dst, overwrite: overwrite !== false }); };
  fso.MoveFile = function (src, dst) { rpc('fs/move', { src: src, dst: dst }); };
  fso.GetAbsolutePathName = function (path) { return rpc('fs/abs', { path: path || '' }).abs; };
  fso.GetParentFolderName = function (path) { return getParentFolderName(path); };
  fso.GetFileName = function (path) { return getFileName(path); };
  fso.GetBaseName = function (path) { return getBaseName(path); };
  fso.GetExtensionName = function (path) { return getExtensionName(path); };
  fso.BuildPath = function (a, b) {
    var base = String(a || '').replace(/[\/\\]+$/, '');
    var tail = String(b || '').replace(/^[\/\\]+/, '');
    return base + '/' + tail;
  };
  fso.GetTempName = function () {
    return 'rad' + Math.floor(Math.random() * 0xffff).toString(16).toUpperCase() + '.tmp';
  };
  fso.GetSpecialFolder = function (which) {
    if (which === 2) { return '/tmp'; }
    return rpc('shell/specialfolder', { name: 'windows' }).path;
  };
  fso.Drives = makeCollection([{ Path: 'C:', DriveLetter: 'C', IsReady: true,
    FreeSpace: 0, TotalSize: 0, FileSystem: 'NTFS' }]);
  fso.CreateTextFile = function (path, overwrite, unicode) {
    var absolute = rpc('fs/abs', { path: path }).abs;
    // Windows throws "File already exists" when overwrite is omitted, but the
    // BIR shell re-saves the profile XML on every step with the argument
    // omitted, so treat an existing file as replaceable (the intent is clear)
    // and record it for traceability.
    if (!overwrite && fso.FileExists(path)) {
      hostLog('debug', 'CreateTextFile replacing existing file: ' + path);
    }
    rpc('fs/write', { path: path, text: '', encoding: unicode ? 'utf-16-le' : 'utf-8' });
    return new TextStream({ path: absolute, mode: FOR_WRITING, text: '',
      unicode: !!unicode, append: false });
  };
  fso.OpenTextFile = function (path, iomode, create, format) {
    var mode = iomode === undefined ? FOR_READING : iomode;
    var unicode = format === TRISTATE_UNICODE;
    var absolute = rpc('fs/abs', { path: path }).abs;
    if (mode === FOR_READING) {
      var read = rpc('fs/read', { path: path });
      return new TextStream({ path: absolute, mode: FOR_READING, text: read.text,
        append: false, encoding: read.encoding });
    }
    if (mode === FOR_APPENDING) {
      var existing = '';
      var exists = fso.FileExists(path);
      if (exists) { existing = rpc('fs/read', { path: path }).text; }
      else if (!create) { throw new Error('File not found: ' + path); }
      return new TextStream({ path: absolute, mode: FOR_APPENDING, text: existing,
        unicode: unicode, append: exists });
    }
    if (!create && !fso.FileExists(path)) { throw new Error('File not found: ' + path); }
    return new TextStream({ path: absolute, mode: FOR_WRITING, text: '',
      unicode: unicode, append: false });
  };

  window.FileSystemObject = FileSystemObject;
  window.__ebirFSO = fso;

  /* ------------------------------------------------------ XMLDOM objects */
  function XMLNodeList(nodes) {
    var list = [];
    for (var i = 0; i < nodes.length; i++) { list.push(nodes[i]); }
    list.length = nodes.length;
    list.item = function (index) { return this[index] || null; };
    return list;
  }
  function xpathAll(doc, expr) {
    var out = [];
    if (!doc) { return XMLNodeList(out); }
    try {
      var result = doc.evaluate(expr, doc, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      for (var i = 0; i < result.snapshotLength; i++) { out.push(result.snapshotItem(i)); }
    } catch (e) {
      hostLog('warn', 'xpath failed: ' + expr + ' (' + e.message + ')');
    }
    return XMLNodeList(out);
  }
  function XMLDocument() {
    this.async = false;
    this.preserveWhiteSpace = true;
    this.validateOnParse = false;
    this.resolveExternals = false;
    this.readyState = 0;
    this.parseError = { errorCode: 0, reason: '', line: 0, linepos: 0, srcText: '', url: '' };
    this.__doc = null;
  }
  XMLDocument.prototype.__parse = function (text) {
    var parser = new DOMParser();
    var doc = null;
    var err = null;
    try { doc = parser.parseFromString(text, 'text/xml'); } catch (e) { err = e; }
    if (doc) {
      var pe = doc.getElementsByTagName('parsererror');
      if (pe && pe.length) {
        err = pe[0].textContent;
        doc = null;
      }
    }
    this.readyState = 4;
    if (doc) {
      this.__doc = doc;
      this.parseError = { errorCode: 0, reason: '', line: 0, linepos: 0, srcText: text, url: '' };
      return true;
    }
    this.__doc = null;
    this.parseError = { errorCode: -1072896658, reason: String(err || 'parse error'),
      line: 0, linepos: 0, srcText: text, url: '' };
    return false;
  };
  XMLDocument.prototype.load = function (url) {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', url, false);
    try { xhr.send(null); } catch (e) {
      this.parseError = { errorCode: -1, reason: String(e), line: 0, linepos: 0, srcText: '', url: url };
      return false;
    }
    if (xhr.status !== 200 && xhr.status !== 0) {
      this.parseError = { errorCode: -1, reason: 'HTTP ' + xhr.status, line: 0, linepos: 0, srcText: '', url: url };
      return false;
    }
    return this.__parse(xhr.responseText);
  };
  XMLDocument.prototype.loadXML = function (text) { return this.__parse(String(text)); };
  XMLDocument.prototype.setProperty = function () {};
  XMLDocument.prototype.getProperty = function () { return ''; };
  XMLDocument.prototype.selectNodes = function (expr) { return xpathAll(this.__doc, expr); };
  XMLDocument.prototype.selectSingleNode = function (expr) {
    var list = xpathAll(this.__doc, expr);
    return list.length ? list[0] : null;
  };
  Object.defineProperty(XMLDocument.prototype, 'documentElement', {
    get: function () { return this.__doc ? this.__doc.documentElement : null; }
  });
  Object.defineProperty(XMLDocument.prototype, 'childNodes', {
    get: function () { return this.__doc ? this.__doc.childNodes : []; }
  });
  Object.defineProperty(XMLDocument.prototype, 'xml', {
    get: function () { return this.__doc ? new XMLSerializer().serializeToString(this.__doc) : ''; }
  });
  Object.defineProperty(XMLDocument.prototype, 'text', {
    get: function () { return this.__doc ? this.__doc.textContent : ''; }
  });
  XMLDocument.prototype.getElementsByTagName = function (tag) {
    return this.__doc ? this.__doc.getElementsByTagName(tag) : [];
  };

  /* -------------------------------------------------- WScript.Shell etc. */
  var ENV_CACHE = null;
  function envMap() {
    if (!ENV_CACHE) { ENV_CACHE = rpc('host/env', {}).env; }
    return ENV_CACHE;
  }
  function WScriptShell() {}
  WScriptShell.prototype.Run = function (cmd, style, wait) {
    var res = rpc('shell/run', { cmd: String(cmd), wait: wait !== false && wait !== 0 });
    return res.code === undefined ? 0 : res.code;
  };
  WScriptShell.prototype.Exec = function (cmd) {
    var res = rpc('shell/run', { cmd: String(cmd), wait: true, capture: true });
    return {
      StdOut: { ReadAll: function () { return res.stdout || ''; }, Close: function () {} },
      StdErr: { ReadAll: function () { return res.stderr || ''; }, Close: function () {} },
      ExitCode: res.code, Status: 0, ProcessID: 0,
      Terminate: function () {}, Wait: function () { return res.code; }
    };
  };
  WScriptShell.prototype.ExpandEnvironmentStrings = function (text) {
    var env = envMap();
    return String(text === undefined ? '' : text).replace(/%([^%]+)%/g, function (match, name) {
      var value = env[String(name).toUpperCase()];
      return value === undefined ? match : value;
    });
  };
  WScriptShell.prototype.SpecialFolders = function (name) {
    return rpc('shell/specialfolder', { name: name || '' }).path;
  };
  WScriptShell.prototype.Popup = function (text, seconds, title) {
    alert((title ? title + '\n\n' : '') + String(text));
    return 1;
  };
  WScriptShell.prototype.RegRead = function () { return ''; };
  WScriptShell.prototype.RegWrite = function () {};
  WScriptShell.prototype.RegDelete = function () {};
  Object.defineProperty(WScriptShell.prototype, 'CurrentDirectory', {
    get: function () { return rpc('host/env', {}).workdir; },
    set: function () {}
  });

  function ShellApplication() {}
  ShellApplication.prototype.NameSpace = function (dir) {
    var listing = rpc('fs/list', { path: dir || '.' });
    var folder = new FolderObject(listing.abs, listing.entries);
    folder.Self = folder;
    folder.ParseName = function (name) {
      var stat = rpc('fs/stat', { path: String(dir || '.') + '/' + name });
      return stat.is_dir ? new FolderObject(stat.abs || stat.path, []) : new FileObject(stat.abs || stat.path, stat);
    };
    folder.Items = function () { return folder.Files; };
    folder.CopyHere = function () {};
    folder.MoveHere = function () {};
    return folder;
  };
  ShellApplication.prototype.Explore = function () {};
  ShellApplication.prototype.Open = function () {};
  ShellApplication.prototype.ShellExecute = function (file) {
    return rpc('shell/run', { cmd: String(file), wait: false }).code || 0;
  };

  /* ------------------------------------------------- ActiveXObject / glue */
  var COM_FACTORIES = {
    'scripting.filesystemobject': function () { return fso; },
    'scripting.dictionary': function () { return makeDictionary(); },
    'microsoft.xmldom': function () { return ci(new XMLDocument()); },
    'msxml2.domdocument': function () { return ci(new XMLDocument()); },
    'msxml.domdocument': function () { return ci(new XMLDocument()); },
    'msxml2.domdocument.3.0': function () { return ci(new XMLDocument()); },
    'msxml2.domdocument.6.0': function () { return ci(new XMLDocument()); },
    'microsoft.xmlhttp': function () { return new XMLHttpRequest(); },
    'msxml2.xmlhttp': function () { return new XMLHttpRequest(); },
    'msxml2.xmlhttp.3.0': function () { return new XMLHttpRequest(); },
    'msxml2.xmlhttp.6.0': function () { return new XMLHttpRequest(); },
    'msxml2.serverxmlhttp': function () { return new XMLHttpRequest(); },
    'wscript.shell': function () { return ci(new WScriptShell()); },
    'wscript.network': function () { return { UserName: envMap().USERNAME || '', ComputerName: envMap().COMPUTERNAME || '' }; },
    'shell.application': function () { return ci(new ShellApplication()); }
  };

  function makeDictionary() {
    var store = {};
    var dict = {
      Count: 0,
      Add: function (key, value) {
        if (store.hasOwnProperty(String(key).toLowerCase())) {
          throw new Error('This key is already associated with an element of this collection');
        }
        store[String(key).toLowerCase()] = { key: key, value: value };
        dict.Count += 1;
      },
      Exists: function (key) { return store.hasOwnProperty(String(key).toLowerCase()); },
      Items: function () { return { Count: dict.Count }; },
      Keys: function () { return { Count: dict.Count }; },
      Remove: function (key) {
        if (store.hasOwnProperty(String(key).toLowerCase())) { delete store[String(key).toLowerCase()]; dict.Count -= 1; }
      },
      RemoveAll: function () { store = {}; dict.Count = 0; },
      __get: function (key) { return store[String(key).toLowerCase()].value; },
      __set: function (key, value) {
        var k = String(key).toLowerCase();
        if (store.hasOwnProperty(k)) { store[k].value = value; }
        else { dict.Add(key, value); }
      }
    };
    window.__ebirDicts.push(dict);
    return dict;
  }
  window.__ebirDicts = [];

  // IE allowed dictionary access by key: dict("a"); emulate via callable proxy.
  function wrapDictionary(dict) {
    var callable = function (key, value) {
      if (value === undefined) { return dict.__get(key); }
      dict.__set(key, value);
      return value;
    };
    Object.keys(dict).forEach(function (k) {
      if (typeof dict[k] !== 'function') { callable[k] = dict[k]; }
      else { callable[k] = dict[k]; }
    });
    callable.__dict = dict;
    return callable;
  }

  function ActiveXObject(progid) {
    var key = String(progid || '').toLowerCase();
    var factory = COM_FACTORIES[key];
    if (!factory) {
      hostLog('warn', 'ActiveXObject not implemented: ' + progid);
      throw new Error('Automation server cannot create object: ' + progid);
    }
    var obj = factory();
    return obj;
  }
  window.ActiveXObject = ActiveXObject;
  window.CreateObject = function (progid) { return ActiveXObject(progid); };
  window.GetObject = function (progid) { return ActiveXObject(progid); };
  window.Enumerator = function (collection) {
    var index = 0;
    var items = collection && collection._items ? collection._items : (collection || []);
    return {
      atEnd: function () { return index >= (collection ? collection.Count : 0); },
      item: function () { return items[index]; },
      moveFirst: function () { index = 0; },
      moveNext: function () { index += 1; }
    };
  };
  window.__ebirEnum = window.Enumerator;

  /* -------------------------------------------------- window.external */
  window.external = new Proxy({
    value: '',
    __hostCall: function (name) {
      return function () {
        if (name === 'value') { return this.value; }
        hostLog('warn', 'window.external.' + name + ' is not implemented');
        return '';
      };
    }
  }, {
    get: function (target, prop) {
      if (prop in target) { return target[prop]; }
      if (typeof prop !== 'string') { return undefined; }
      return function () { hostLog('warn', 'window.external.' + prop + '() ignored'); return ''; };
    },
    set: function (target, prop, value) { target[prop] = value; return true; }
  });

  /* ------------------------------------------------------ IE leftovers */
  // <HTA:APPLICATION ...> is an unknown element under Chromium, so its
  // attributes are not exposed as properties (the shell reads
  // BIRViewOfflineForms.version). Mirror the attributes IE exposed.
  function installHtaApplication() {
    var el = document.querySelector('hta\\:application') ||
             (document.getElementById && document.getElementById('BIRViewOfflineForms'));
    if (!el || el.__ebirShimmed) { return; }
    el.__ebirShimmed = true;
    var camel = function (name) {
      return String(name).toLowerCase().replace(/-([a-z])/g, function (m, c) { return c.toUpperCase(); });
    };
    var attrs = el.attributes || [];
    for (var i = 0; i < attrs.length; i++) {
      (function (attr) {
        var prop = camel(attr.name);
        if (!prop || prop === 'id') { return; }
        try {
          Object.defineProperty(el, prop, {
            configurable: true,
            get: function () { return attr.value; },
            set: function (value) { attr.value = value; }
          });
        } catch (e) { /* duplicate/unsupported property: ignore */ }
      })(attrs[i]);
    }
    var defaults = {
      commandLine: '', currentDirectory: '', border: 'none', borderStyle: 'normal',
      caption: 'yes', contextMenu: 'yes', maximizeButton: 'yes', minimizeButton: 'yes',
      scroll: 'yes', scrollFlat: 'no', selection: 'yes', innerBorder: 'no',
      singleInstance: 'no', sysMenu: 'yes', windowState: 'normal', navigable: 'no',
      showInTaskBar: 'yes', icon: '', version: '1.0.0', applicationName: ''
    };
    Object.keys(defaults).forEach(function (prop) {
      if (el[prop] === undefined) {
        try {
          Object.defineProperty(el, prop, {
            configurable: true, get: function () { return defaults[prop]; }
          });
        } catch (e) { /* ignore */ }
      }
    });
    if (el.id) { try { window[el.id] = el; } catch (e) { /* ignore */ } }
    hostLog('info', 'HTA:APPLICATION shim installed (id=' + el.id + ', version=' + el.version + ')');
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', installHtaApplication);
  } else {
    installHtaApplication();
  }

  if (!Element.prototype.attachEvent) {
    Element.prototype.attachEvent = function (name, handler) {
      this.addEventListener(String(name).replace(/^on/, ''), handler);
      return true;
    };
    Element.prototype.detachEvent = function (name, handler) {
      this.removeEventListener(String(name).replace(/^on/, ''), handler);
      return true;
    };
  }
  if (!document.attachEvent) {
    document.attachEvent = Element.prototype.attachEvent;
    document.detachEvent = Element.prototype.detachEvent;
  }

  // window.print()/execCommand('Print') -> native printer via the host.
  window.print = function () {
    try { rpc('host/print', {}); } catch (e) { hostLog('error', 'print failed: ' + e.message); }
  };
  var nativeExecCommand = document.execCommand ? document.execCommand.bind(document) : null;
  document.execCommand = function (command, ui, value) {
    var cmd = String(command || '').toLowerCase();
    if (cmd === 'print') { window.print(); return true; }
    if (cmd === 'saveas') { return false; }
    if (nativeExecCommand) {
      try { return nativeExecCommand(command, ui, value); } catch (e) { return false; }
    }
    return false;
  };

  // IE-only property shims that a couple of form scripts touch.
  try {
    Object.defineProperty(HTMLElement.prototype, 'filters', {
      configurable: true,
      get: function () { return { length: 0, item: function () { return null; } }; }
    });
    Object.defineProperty(HTMLElement.prototype, 'runtimeStyle', {
      configurable: true,
      get: function () {
        if (!this.__runtimeStyle) { this.__runtimeStyle = {}; }
        return this.__runtimeStyle;
      }
    });
  } catch (e) { /* non-fatal */ }

  // IE reports these; a few scripts branch on them.
  try {
    Object.defineProperty(document, 'documentMode', { configurable: true, get: function () { return 9; } });
    Object.defineProperty(document, 'compatMode', { configurable: true, get: function () { return 'CSS1Compat'; } });
  } catch (e) { /* non-fatal */ }

  // window chrome helpers IE exposed to HTAs.
  window.resizeTo = window.resizeTo || function () {};
  window.moveTo = window.moveTo || function () {};
  window.focus = window.focus || function () {};
  window.showModalDialog = window.showModalDialog || function (url) {
    hostLog('warn', 'showModalDialog(' + url + ') degraded to window.open');
    window.open(url, '_blank', 'width=800,height=600');
    return null;
  };

  // Cursor helpers used by the "Busy" indicator.
  window.setCursor = window.setCursor || function () {};

  hostLog('info', 'eBIRForms compatibility layer ready (' + location.pathname + ')');
})();
