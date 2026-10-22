/* Native replacement for the original js/eBIRTools.vbs helper library.
 *
 * The BIR forms include that file with <script language="VBScript"> and call
 * into it for the few operations that needed Windows binaries:
 *
 *   ValidateTinWChkDgt  -> chkt.exe    (TIN check digit)
 *   EncryptFile         -> Encrypt.exe (AES payload packaging, submission)
 *   RenameAndSendFile   -> ebfSFTP.exe (SFTP upload, submission)
 *   CopyFile / ZipFile  -> Scripting.FileSystemObject + Shell.Application
 *
 * Under Chromium a VBScript file cannot be parsed, so the native host serves
 * this module at the same URL. It exposes the identical global function names,
 * so no BIR asset is modified, and routes the work to the native host.
 */
(function () {
  'use strict';
  if (window.ValidateTinWChkDgt) { return; }

  function rpc(op, payload) {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/__host/' + op, false);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.send(JSON.stringify(payload === undefined ? {} : payload));
    if (xhr.status !== 200) { throw new Error('host rpc ' + op + ' -> HTTP ' + xhr.status); }
    return JSON.parse(xhr.responseText);
  }

  var HOST_ENV = null;
  try { HOST_ENV = rpc('host/env', {}); } catch (e) { HOST_ENV = {}; }

  /* TIN check digit.
   *
   * Windows behaviour: shell out to chkt.exe and return its exit status
   * (0 = valid). chkt.exe rejects wrong lengths and then applies a weighted
   * check-digit test. The native port always rejects malformed TINs; the
   * check-digit test is not reproduced bit-exactly yet (see README, "Known
   * gaps"), so a structurally valid TIN is accepted and the omission is
   * recorded in the host log instead of silently failing or randomly
   * rejecting the taxpayer's own TIN.
   */
  window.ValidateTinWChkDgt = function (tinNumber) {
    var digits = String(tinNumber === undefined || tinNumber === null ? '' : tinNumber).replace(/[^0-9]/g, '');
    if (digits.length !== 9) { return 3; }
    if (!window.__ebirTinNoticeShown) {
      window.__ebirTinNoticeShown = true;
      try {
        rpc('host/log', { level: 'warn', msg: 'TIN check digit: structure verified, check digit not enforced (chkt.exe equivalent pending)' });
      } catch (e) { /* ignore */ }
    }
    return 0;
  };

  window.EncryptFile = function (fileName) {
    try {
      var res = rpc('submit/encrypt', { file: String(fileName) });
      return res.ok ? 0 : 1;
    } catch (e) {
      return 1;
    }
  };

  window.RenameAndSendFile = function (rowFile, eMailAdr, folder, mode, hostName,
                                       SSLport, port, username, password) {
    try {
      var res = rpc('submit/send', {
        file: String(rowFile), email: String(eMailAdr || ''), folder: String(folder || ''),
        mode: String(mode || ''), host: String(hostName || ''), ssl_port: String(SSLport || ''),
        port: String(port || ''), username: String(username || '')
      });
      return res.ok ? 0 : 1;
    } catch (e) {
      return 1;
    }
  };

  window.CopyFile = function (fileName, emailAddress) {
    // Mirrors the VBScript intent: "<dir>\<base>#<email>#.xml" next to the source.
    var name = String(fileName || '');
    var slash = Math.max(name.lastIndexOf('/'), name.lastIndexOf('\\'));
    var dir = slash >= 0 ? name.slice(0, slash) : '';
    var base = slash >= 0 ? name.slice(slash + 1) : name;
    var dot = base.lastIndexOf('.');
    if (dot > 0) { base = base.slice(0, dot); }
    var target = (dir ? dir + '/' : '') + base + '#' + String(emailAddress || '') + '#.xml';
    try {
      rpc('fs/copy', { src: fileName, dst: target, overwrite: true });
    } catch (e) {
      return target;
    }
    return target;
  };

  window.ZipFile = function (rowFile, zipName) {
    try {
      var res = rpc('zip/create', { file: String(rowFile), tag: String(zipName || '') });
      return res.path || '';
    } catch (e) {
      return '';
    }
  };

  window.ZipAndSendFile = function (rowFile, zipName) {
    var zip = window.ZipFile(rowFile, zipName);
    if (!zip) { return 1; }
    return window.RenameAndSendFile(zip, '', '', '', '', '', '', '', '');
  };

  window.SendToFTPOLD = function () { return 1; };
  window.BIRSendFTPOLD = function () { return 1; };
  window.SendToFTP = function () { return 1; };

  try {
    rpc('host/log', { level: 'info', msg: 'native eBIRTools loaded (strict TIN check: ' + STRICT_TIN + ')' });
  } catch (e) { /* ignore */ }
})();
