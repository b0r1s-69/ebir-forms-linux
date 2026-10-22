"""Native Linux port of the BIR "Offline eBIRForms Package".

The original Windows application (``BIRForms.exe``) is a thin Microsoft
Internet Explorer host; every BIR form and all of its business logic ship as
HTA/JScript assets inside that executable's resource section.  This package
re-hosts those unmodified assets on a native Chromium (QtWebEngine) window and
supplies the Windows automation objects the scripts expect
(Scripting.FileSystemObject, Microsoft.XMLDOM, WScript.Shell, ...), so the
forms run natively on Linux without Wine and without any Microsoft binaries.
"""

__version__ = "1.0.0"
