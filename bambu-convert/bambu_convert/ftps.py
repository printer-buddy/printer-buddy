import calendar
import ftplib
import io
import posixpath
import ssl
import time


class _ImplicitTLS(ftplib.FTP_TLS):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    # the printer wants the data connection to reuse the control connection's TLS session
    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


class Files:
    """The printer's SD card / storage over implicit FTPS (port 990, user bblp)."""

    def __init__(self, host: str, access_code: str):
        self.host, self.code = host, access_code

    def _open(self) -> ftplib.FTP_TLS:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ftp = _ImplicitTLS(context=ctx, timeout=20)
        ftp.connect(self.host, 990)
        ftp.login("bblp", self.code)
        ftp.prot_p()
        return ftp

    def listdir(self, path: str) -> tuple[list[dict], list[dict]]:
        dirs, files = [], []
        with self._open() as ftp:
            try:
                entries = list(ftp.mlsd(path or "/", facts=["type", "size", "modify"]))
            except ftplib.error_perm:
                entries = [(n, {}) for n in ftp.nlst(path or "/")]
            for name, facts in entries:
                name = posixpath.basename(name)
                if name in (".", "..") or not name:
                    continue
                mod = facts.get("modify")
                ts = float(calendar.timegm(time.strptime(mod[:14], "%Y%m%d%H%M%S"))) if mod else 0.0
                item = {"name": name, "modified": ts, "size": int(facts.get("size", 0) or 0)}
                (dirs if facts.get("type") == "dir" else files).append(item)
        return dirs, files

    def read(self, path: str) -> bytes:
        buf = io.BytesIO()
        with self._open() as ftp:
            ftp.retrbinary(f"RETR {path}", buf.write)
        return buf.getvalue()

    def write(self, path: str, data: bytes):
        with self._open() as ftp:
            ftp.storbinary(f"STOR {path}", io.BytesIO(data))

    def delete(self, path: str):
        with self._open() as ftp:
            ftp.delete(path)

    def rename(self, src: str, dst: str):
        with self._open() as ftp:
            ftp.rename(src, dst)

    def mkdir(self, path: str):
        with self._open() as ftp:
            ftp.mkd(path)

    def rmdir(self, path: str):
        with self._open() as ftp:
            ftp.rmd(path)
