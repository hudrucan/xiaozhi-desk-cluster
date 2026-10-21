use anyhow::{bail, ensure, Context, Result};
use base64::{engine::general_purpose::STANDARD, Engine};
use flate2::{write::GzEncoder, Compression, GzBuilder};
use rusqlite::{backup::Backup, Connection, OpenFlags};
use std::{
    fs::{self, File, Permissions},
    io::{self, BufRead, BufReader, Read, Seek, SeekFrom, Write},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};
use tempfile::{NamedTempFile, TempDir};

const LIMIT: u64 = 16 * 1024 * 1024;
const DBLIMIT: u64 = 64 * 1024 * 1024;
const SMALL: u64 = 1024 * 1024;
const CHUNK: usize = 3 * 65536;

struct Worker {
    action: String,
    data: PathBuf,
    scratch: TempDir,
    bridge: PathBuf,
    ha: PathBuf,
    python: String,
    owner: (u32, u32),
    cancelled: Arc<AtomicBool>,
    deadline: Instant,
}
impl Worker {
    fn check(&self) -> Result<()> {
        ensure!(!self.cancelled.load(Ordering::Relaxed), "cancelled");
        ensure!(Instant::now() < self.deadline, "deadline exceeded");
        Ok(())
    }
    fn helper(&self, action: &str, input: &[u8]) -> Result<Vec<u8>> {
        self.check()?;
        let mut incoming = tempfile::tempfile_in(self.scratch.path())?;
        incoming.write_all(input)?;
        incoming.seek(SeekFrom::Start(0))?;
        let mut out = tempfile::tempfile_in(self.scratch.path())?;
        let mut child = Command::new(&self.python)
            .arg(&self.bridge)
            .arg(action)
            .arg(&self.ha)
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .stdin(Stdio::from(incoming))
            .stdout(Stdio::from(out.try_clone()?))
            .stderr(Stdio::null())
            .spawn()?;
        let result = (|| -> Result<()> {
            loop {
                self.check()?;
                ensure!(out.metadata()?.len() <= 4 * SMALL, "helper output limit");
                if let Some(status) = child.try_wait()? {
                    ensure!(status.success(), "compatibility helper failed");
                    break;
                }
                std::thread::sleep(Duration::from_millis(5));
            }
            Ok(())
        })();
        if result.is_err() {
            let _ = child.kill();
            let _ = child.wait();
        }
        result?;
        self.check()?;
        out.seek(SeekFrom::Start(0))?;
        let mut bytes = Vec::new();
        out.read_to_end(&mut bytes)?;
        Ok(bytes)
    }
    fn small(&self, path: &Path) -> Result<Vec<u8>> {
        let mut bytes = Vec::new();
        File::open(path)?.take(SMALL + 1).read_to_end(&mut bytes)?;
        ensure!(bytes.len() as u64 <= SMALL, "configuration limit");
        Ok(bytes)
    }
    fn workflows(&self, bytes: &Option<Vec<u8>>) -> Result<()> {
        if let Some(b) = bytes {
            ensure!(b.len() as u64 <= SMALL, "workflow limit");
            self.helper("workflows", b)?;
        }
        Ok(())
    }
    fn optional(&self, path: &Path) -> Result<Option<Vec<u8>>> {
        if path.try_exists()? {
            Ok(Some(self.small(path)?))
        } else {
            Ok(None)
        }
    }
    fn integrity(&self, path: &Path) -> Result<()> {
        self.check()?;
        let c = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
            .context("read-only SQLite open")?;
        c.busy_timeout(Duration::from_secs(3))?;
        c.pragma_update(None, "cache_size", -2048)?;
        let cancelled = self.cancelled.clone();
        let deadline = self.deadline;
        c.progress_handler(
            1000,
            Some(move || cancelled.load(Ordering::Relaxed) || Instant::now() >= deadline),
        );
        let result: String = c.query_row("PRAGMA quick_check", [], |r| r.get(0))?;
        ensure!(result == "ok", "SQLite integrity failed");
        self.check()
    }
    fn create(&self) -> Result<()> {
        let conf_path = self.data.join("config/app.conf");
        let workflow_path = self.data.join("config/workflows.json");
        let conf = self.small(&conf_path)?;
        let workflows = self.optional(&workflow_path)?;
        self.workflows(&workflows)?;
        let db = NamedTempFile::new_in(self.scratch.path())?;
        {
            let src = Connection::open_with_flags(
                self.data.join("db/app.db"),
                OpenFlags::SQLITE_OPEN_READ_ONLY,
            )
            .context("read-only SQLite open")?;
            src.busy_timeout(Duration::from_secs(3))?;
            src.pragma_update(None, "cache_size", -2048)?;
            let mut dst = Connection::open(db.path()).context("backup destination")?;
            dst.pragma_update(None, "cache_size", -2048)?;
            let backup = Backup::new(&src, &mut dst).context("backup init")?;
            loop {
                self.check()?;
                match backup.step(256).context("backup step")? {
                    rusqlite::backup::StepResult::Done => break,
                    rusqlite::backup::StepResult::More => {}
                    _ => std::thread::sleep(Duration::from_millis(10)),
                }
                ensure!(fs::metadata(db.path())?.len() <= DBLIMIT, "database limit");
            }
        }
        ensure!(db.as_file().metadata()?.len() <= DBLIMIT, "database limit");
        self.integrity(db.path())?;
        ensure!(
            conf == self.small(&conf_path)? && workflows == self.optional(&workflow_path)?,
            "configuration changed during snapshot"
        );
        // Python 3.11's OS=3 differs from Python 3.13+'s OS=255. Ask the
        // co-located interpreter, rather than introducing a format setting.
        let header = self.helper("gzip-header", b"")?;
        ensure!(header.len() == 10, "gzip header");
        let mut output = tempfile::tempfile_in(self.scratch.path())?;
        {
            let mut gz: GzEncoder<_> = GzBuilder::new()
                .mtime(0)
                .operating_system(header[9])
                .write(&mut output, Compression::fast());
            gz.write_all(b"{\"configuration\":\"")?;
            gz.write_all(STANDARD.encode(&conf).as_bytes())?;
            gz.write_all(b"\",\"database\":\"")?;
            let mut src = BufReader::new(File::open(db.path())?);
            let mut buf = vec![0; CHUNK];
            loop {
                self.check()?;
                let mut n = 0;
                while n < buf.len() {
                    let got = src.read(&mut buf[n..])?;
                    if got == 0 {
                        break;
                    }
                    n += got;
                }
                if n == 0 {
                    break;
                }
                gz.write_all(STANDARD.encode(&buf[..n]).as_bytes())?;
            }
            gz.write_all(b"\"")?;
            if let Some(w) = workflows {
                gz.write_all(b",\"workflows\":\"")?;
                gz.write_all(STANDARD.encode(w).as_bytes())?;
                gz.write_all(b"\"")?;
            }
            gz.write_all(b"}")?;
            gz.finish()?;
        }
        ensure!(output.metadata()?.len() <= LIMIT, "checkpoint limit");
        // Validate before emitting any stdout, just as Python does.
        output.seek(SeekFrom::Start(0))?;
        self.unpack(&mut output)?;
        output.seek(SeekFrom::Start(0))?;
        io::copy(&mut output, &mut io::stdout().lock())?;
        Ok(())
    }
    fn unpack<R: Read + ?Sized>(&self, input: &mut R) -> Result<Payload> {
        let payload = self.read_payload(input)?;
        self.workflows(&payload.workflows)?;
        Ok(payload)
    }
    fn read_payload<R: Read + ?Sized>(&self, input: &mut R) -> Result<Payload> {
        // Spool compressed input under the private run directory. This enforces
        // the compressed ceiling even if gzip finishes before the input does.
        let mut compressed = tempfile::tempfile_in(self.scratch.path())?;
        ensure!(
            io::copy(&mut input.take(LIMIT + 1), &mut compressed)? <= LIMIT,
            "checkpoint limit"
        );
        compressed.seek(SeekFrom::Start(0))?;
        let decoder = PythonGzip::new(compressed);
        let mut parser = Parser {
            reader: BufReader::new(decoder),
            count: 0,
            worker: self,
        };
        let db = NamedTempFile::new_in(self.scratch.path())?;
        let mut conf = None;
        let mut workflows = None;
        let mut database = false;
        parser.expect(b'{')?;
        loop {
            let key = parser.string(64)?;
            parser.expect(b':')?;
            match key.as_slice() {
                b"database" => {
                    let mut file = db.reopen()?;
                    file.set_len(0)?;
                    file.seek(SeekFrom::Start(0))?;
                    parser.base64(&mut file, DBLIMIT)?;
                    database = true;
                }
                b"configuration" => {
                    let mut v = Vec::new();
                    parser.base64(&mut v, SMALL)?;
                    conf = Some(v);
                }
                b"workflows" => {
                    let mut v = Vec::new();
                    parser.base64(&mut v, SMALL)?;
                    workflows = Some(v);
                }
                _ => bail!("unknown checkpoint member"),
            }
            match parser.nonspace()? {
                Some(b',') => {}
                Some(b'}') => break,
                _ => bail!("JSON object"),
            }
        }
        ensure!(parser.nonspace()?.is_none(), "trailing JSON");
        ensure!(database && conf.is_some(), "missing checkpoint member");
        let mut header = [0; 16];
        File::open(db.path())?.read_exact(&mut header)?;
        ensure!(&header == b"SQLite format 3\0", "SQLite header");
        Ok(Payload {
            db,
            conf: conf.unwrap(),
            workflows,
        })
    }
    fn restore(&self, input: &mut dyn BufRead) -> Result<()> {
        let mut header = Vec::new();
        input.take(4097).read_until(b'\n', &mut header)?;
        ensure!(
            header.len() <= 4096 && header.last() == Some(&b'\n'),
            "restore envelope"
        );
        let value: serde_json::Value =
            serde_json::from_slice(&header).context("restore envelope JSON")?;
        let object = value.as_object().context("restore envelope object")?;
        ensure!(object.len() == 1, "restore envelope keys");
        let token = object
            .get("token")
            .and_then(|v| v.as_str())
            .context("restore credentials")?;
        ensure!(
            !token.is_empty() && token.chars().count() <= 2048,
            "restore credentials length"
        );
        let payload = self.read_payload(input)?;
        self.integrity(payload.db.path())?;
        // Only bounded config/workflow bytes enter Python. Validate workflows
        // and apply the authoritative policy in one helper, before target writes.
        let policy_input = serde_json::to_vec(&serde_json::json!({
            "token": token,
            "configuration": STANDARD.encode(&payload.conf),
            "workflows": payload.workflows.as_ref().map(|w| STANDARD.encode(w)),
        }))?;
        let conf = self.helper("restore", &policy_input)?;
        self.check()?;
        for suffix in ["-wal", "-shm"] {
            remove_if_exists(&self.data.join(format!("db/app.db{suffix}")))?;
        }
        self.atomic(
            &self.data.join("db/app.db"),
            &mut File::open(payload.db.path())?,
        )?;
        self.atomic(&self.data.join("config/app.conf"), &mut conf.as_slice())?;
        let workflow_path = self.data.join("config/workflows.json");
        if let Some(w) = payload.workflows {
            self.atomic(&workflow_path, &mut w.as_slice())?;
        } else {
            remove_if_exists(&workflow_path)?;
        }
        for name in ["", "db", "config", "db/app.db", "config/app.conf"] {
            let path = self.data.join(name);
            self.chown(&path)?;
            fs::set_permissions(
                &path,
                Permissions::from_mode(if path.is_dir() { 0o700 } else { 0o600 }),
            )?;
        }
        Ok(())
    }
    fn chown(&self, path: &Path) -> Result<()> {
        use std::os::unix::ffi::OsStrExt;
        let path = std::ffi::CString::new(path.as_os_str().as_bytes())?;
        ensure!(
            unsafe { libc::chown(path.as_ptr(), self.owner.0, self.owner.1) } == 0,
            "ownership failed"
        );
        Ok(())
    }
    fn atomic(&self, path: &Path, input: &mut dyn Read) -> Result<()> {
        let parent = path.parent().context("restore parent")?;
        fs::create_dir_all(parent)?;
        // Same-directory rename works across separate data/scratch filesystems.
        let mut temp = NamedTempFile::new_in(parent)?;
        temp.as_file()
            .set_permissions(Permissions::from_mode(0o600))?;
        let mut buf = [0; 65536];
        loop {
            self.check()?;
            let n = input.read(&mut buf)?;
            if n == 0 {
                break;
            }
            temp.write_all(&buf[..n])?;
        }
        temp.flush()?;
        temp.as_file().sync_all()?;
        self.check()?;
        temp.persist(path).map_err(|e| e.error)?;
        File::open(parent)?.sync_all()?;
        if path.file_name().is_some_and(|n| n == "workflows.json") {
            self.chown(path)?;
        }
        Ok(())
    }
}
struct Payload {
    db: NamedTempFile,
    conf: Vec<u8>,
    workflows: Option<Vec<u8>>,
}
fn remove_if_exists(path: &Path) -> Result<()> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e.into()),
    }
}

// CPython accepts concatenated members and zero padding between/after them.
struct PythonGzip<R: BufRead> {
    member: Option<flate2::bufread::GzDecoder<R>>,
}
impl PythonGzip<BufReader<File>> {
    fn new(file: File) -> Self {
        Self {
            member: Some(flate2::bufread::GzDecoder::new(BufReader::new(file))),
        }
    }
}
impl<R: BufRead> Read for PythonGzip<R> {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        if output.is_empty() {
            return Ok(0);
        }
        loop {
            let Some(member) = self.member.as_mut() else {
                return Ok(0);
            };
            let n = member.read(output)?;
            if n != 0 {
                return Ok(n);
            }
            let mut source = self.member.take().unwrap().into_inner();
            loop {
                let bytes = source.fill_buf()?;
                if bytes.is_empty() {
                    return Ok(0);
                }
                let zeros = bytes.iter().take_while(|b| **b == 0).count();
                if zeros == 0 {
                    break;
                }
                source.consume(zeros);
            }
            self.member = Some(flate2::bufread::GzDecoder::new(source));
        }
    }
}

// Only this envelope is streamed: a JSON object of ASCII base64 strings.
// JSON escapes, member order and duplicate (last-wins) members remain accepted.
struct Parser<'a, R> {
    reader: R,
    count: u64,
    worker: &'a Worker,
}
impl<R: BufRead> Parser<'_, R> {
    fn byte(&mut self) -> Result<Option<u8>> {
        if self.count % 65536 == 0 {
            self.worker.check()?;
        }
        let mut b = [0];
        let n = self.reader.read(&mut b)?;
        if n == 0 {
            return Ok(None);
        }
        self.count += 1;
        ensure!(self.count <= DBLIMIT * 2, "expansion limit");
        Ok(Some(b[0]))
    }
    fn nonspace(&mut self) -> Result<Option<u8>> {
        loop {
            let b = self.byte()?;
            if !matches!(b, Some(b' ' | b'\t' | b'\r' | b'\n')) {
                return Ok(b);
            }
        }
    }
    fn expect(&mut self, expected: u8) -> Result<()> {
        ensure!(self.nonspace()? == Some(expected), "JSON delimiter");
        Ok(())
    }
    fn character(&mut self) -> Result<Option<u8>> {
        let b = self.byte()?.context("truncated JSON string")?;
        match b {
            b'"' => Ok(None),
            b'\\' => {
                let escaped = self.byte()?.context("truncated escape")?;
                let value = match escaped {
                    b'"' | b'\\' | b'/' => escaped,
                    b'b' => 8,
                    b'f' => 12,
                    b'n' => 10,
                    b'r' => 13,
                    b't' => 9,
                    b'u' => {
                        let mut code = 0u32;
                        for _ in 0..4 {
                            code = code * 16
                                + (self.byte()?.context("unicode escape")? as char)
                                    .to_digit(16)
                                    .context("unicode hex")?;
                        }
                        ensure!(code < 128, "non-ASCII envelope string");
                        code as u8
                    }
                    _ => bail!("JSON escape"),
                };
                Ok(Some(value))
            }
            0..=31 | 128..=255 => bail!("non-ASCII envelope string"),
            _ => Ok(Some(b)),
        }
    }
    fn string(&mut self, limit: usize) -> Result<Vec<u8>> {
        self.expect(b'"')?;
        let mut out = Vec::new();
        while let Some(b) = self.character()? {
            ensure!(out.len() < limit, "JSON key limit");
            out.push(b);
        }
        Ok(out)
    }
    fn base64(&mut self, out: &mut dyn Write, limit: u64) -> Result<()> {
        self.expect(b'"')?;
        let mut encoded = Vec::with_capacity(65536);
        let mut total = 0u64;
        let mut padded = false;
        let engine = base64::engine::GeneralPurpose::new(
            &base64::alphabet::STANDARD,
            base64::engine::GeneralPurposeConfig::new().with_decode_allow_trailing_bits(true),
        );
        loop {
            self.worker.check()?;
            let available = self.reader.fill_buf()?;
            ensure!(!available.is_empty(), "truncated base64 string");
            // Canonical base64 has no JSON escapes. Consume contiguous spans
            // directly; escaped strings still use the same checked JSON path.
            let n = available
                .iter()
                .position(|b| matches!(*b, b'"' | b'\\'))
                .unwrap_or(available.len())
                .min(65536 - encoded.len());
            if n > 0 {
                ensure!(
                    !padded || available[..n].iter().all(|b| *b == b'='),
                    "excess base64 data"
                );
                padded |= available[..n].contains(&b'=');
                encoded.extend_from_slice(&available[..n]);
                self.reader.consume(n);
                self.count += n as u64;
                ensure!(self.count <= DBLIMIT * 2, "expansion limit");
            } else {
                let Some(b) = self.character()? else {
                    break;
                };
                ensure!(!padded || b == b'=', "excess base64 data");
                padded |= b == b'=';
                encoded.push(b);
            }
            if encoded.len() == 65536 {
                let raw = engine.decode(&encoded)?;
                total += raw.len() as u64;
                ensure!(total <= limit, "payload limit");
                out.write_all(&raw)?;
                encoded.clear();
            }
        }
        // CPython accepts redundant '=' after complete unpadded quartets.
        if let Some(first) = encoded.iter().position(|b| *b == b'=') {
            if first % 4 == 0 {
                encoded.truncate(first);
            }
        }
        let raw = engine.decode(&encoded)?;
        ensure!(total + raw.len() as u64 <= limit, "payload limit");
        out.write_all(&raw)?;
        Ok(())
    }
}
fn run() -> Result<()> {
    let mut args = std::env::args().skip(1);
    let mut action = args.next().context("operation required")?;
    if action == "--snapshot-worker" {
        action = args.next().context("operation required")?;
    }
    ensure!(
        ["create", "validate", "restore"].contains(&action.as_str()),
        "unknown operation"
    );
    let mut data = PathBuf::new();
    let mut run = std::env::temp_dir();
    let mut bridge = std::env::current_exe()?.with_file_name("bridge.py");
    let mut ha = PathBuf::from("/usr/local/libexec/desk-network/ha.py");
    let mut python = String::from("python3");
    let mut owner = (20211, 20211);
    while let Some(arg) = args.next() {
        let v = args.next().context("option value required")?;
        match arg.as_str() {
            "--data" => data = v.into(),
            "--run" => run = v.into(),
            "--bridge" => bridge = v.into(),
            "--ha" => ha = v.into(),
            "--python" => python = v,
            "--owner" => {
                let (uid, gid) = v.split_once(':').context("owner uid:gid")?;
                owner = (uid.parse()?, gid.parse()?);
            }
            _ => bail!("unknown option"),
        }
    }
    ensure!(
        action == "validate" || !data.as_os_str().is_empty(),
        "data path required"
    );
    fs::create_dir_all(&run)?;
    let scratch = tempfile::Builder::new()
        .prefix("ckpt-rust-")
        .tempdir_in(run)?;
    let cancelled = Arc::new(AtomicBool::new(false));
    for sig in [signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT] {
        signal_hook::flag::register(sig, cancelled.clone())?;
    }
    let worker = Worker {
        action,
        data,
        scratch,
        bridge,
        ha,
        python,
        owner,
        cancelled,
        deadline: Instant::now() + Duration::from_secs(50),
    };
    match worker.action.as_str() {
        "create" => worker.create(),
        "validate" => worker.unpack(&mut io::stdin().lock()).map(|_| ()),
        "restore" => worker.restore(&mut io::stdin().lock()),
        _ => unreachable!(),
    }
}
fn main() {
    if run().is_err() {
        // No parser values, paths or SQLite diagnostics: these can hold secrets.
        eprintln!("desk-checkpoint: operation failed");
        std::process::exit(1);
    }
}
