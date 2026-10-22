//! Port of desk_monitoring/files/config_journal.py, single-decree Paxos per revision.
//! Promise/accept/commit are fsynced; a proposer must carry the highest accepted value.
use aes_gcm::{
    aead::{Aead, KeyInit},
    Aes256Gcm, Nonce,
};
use anyhow::{bail, ensure, Result};
use hmac::{Hmac, Mac};
use rand::RngCore;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs::{self, File, OpenOptions},
    io::Write,
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
};

pub type Ballot = (u64, String);

pub fn bytes(value: &impl Serialize) -> Vec<u8> {
    // Value maps are sorted, matching the reference journal's canonical JSON.
    serde_json::to_vec(&serde_json::to_value(value).expect("serializable state"))
        .expect("JSON state")
}
pub fn digest(value: &impl Serialize) -> String {
    hex::encode(Sha256::digest(bytes(value)))
}
pub fn sign(key: &[u8], data: &[u8]) -> String {
    let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(key).expect("HMAC key");
    mac.update(data);
    hex::encode(mac.finalize().into_bytes())
}
pub fn verify(key: &[u8], data: &[u8], signature: &str) -> bool {
    let Ok(signature) = hex::decode(signature) else {
        return false;
    };
    let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(key).expect("HMAC key");
    mac.update(data);
    mac.verify_slice(&signature).is_ok()
}
pub fn atomic(path: &Path, raw: &[u8]) -> Result<()> {
    let pending = path.with_extension("pending");
    let mut file = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&pending)?;
    file.write_all(raw)?;
    file.sync_all()?;
    fs::rename(&pending, path)?;
    File::open(path.parent().expect("state directory"))?.sync_all()?;
    Ok(())
}
pub fn encrypt(key: &[u8; 32], raw: &[u8]) -> Result<Vec<u8>> {
    let mut nonce = [0u8; 12];
    rand::thread_rng().fill_bytes(&mut nonce);
    let encrypted = Aes256Gcm::new_from_slice(key)
        .unwrap()
        .encrypt(Nonce::from_slice(&nonce), raw)
        .map_err(|_| anyhow::anyhow!("state encryption failed"))?;
    Ok([nonce.to_vec(), encrypted].concat())
}
pub fn decrypt(key: &[u8; 32], raw: &[u8]) -> Result<Vec<u8>> {
    ensure!(
        raw.len() >= 28 && raw.len() <= 2 * 1024 * 1024,
        "invalid encrypted checkpoint"
    );
    Aes256Gcm::new_from_slice(key)
        .unwrap()
        .decrypt(Nonce::from_slice(&raw[..12]), &raw[12..])
        .map_err(|_| anyhow::anyhow!("invalid encrypted checkpoint"))
}
#[derive(Clone, Serialize, Deserialize)]
pub struct Accepted {
    pub ballot: Ballot,
    pub value: Value,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct Receipt {
    pub node: String,
    pub revision: i64,
    pub ballot: Ballot,
    pub digest: String,
    pub signature: String,
}
impl Receipt {
    fn message(&self) -> Value {
        json!({"node":self.node,"revision":self.revision,"ballot":self.ballot,"digest":self.digest})
    }
}
#[derive(Clone, Serialize, Deserialize)]
pub struct Head {
    pub revision: i64,
    pub value: Value,
    pub certificate: Vec<Receipt>,
    pub promised: Ballot,
    pub accepted: Option<Accepted>,
    pub applied_revision: i64,
}
pub struct Journal {
    pub head: Head,
    pub node: String,
    members: BTreeSet<String>,
    key: [u8; 32],
    path: PathBuf,
}
impl Journal {
    pub fn open(
        path: PathBuf,
        key: [u8; 32],
        node: String,
        members: BTreeSet<String>,
        seed: Value,
    ) -> Result<Self> {
        let head = if path.exists() {
            serde_json::from_slice(&decrypt(&key, &fs::read(&path)?)?)?
        } else {
            Head {
                revision: 0,
                value: seed.clone(),
                certificate: vec![],
                promised: (0, String::new()),
                accepted: None,
                applied_revision: -1,
            }
        };
        ensure!(
            head.revision != 0 || digest(&head.value) == digest(&seed),
            "initial auth seed differs"
        );
        let journal = Self {
            head,
            node,
            members,
            key,
            path,
        };
        journal.save()?;
        Ok(journal)
    }
    fn save(&self) -> Result<()> {
        atomic(&self.path, &encrypt(&self.key, &bytes(&self.head))?)
    }
    pub fn promise(&mut self, revision: i64, ballot: Ballot) -> Result<Value> {
        if revision != self.head.revision + 1 {
            return Ok(json!({"head":self.head}));
        }
        ensure!(
            ballot > self.head.promised,
            "configuration ballot superseded"
        );
        self.head.promised = ballot;
        self.save()?;
        Ok(json!({"accepted":self.head.accepted}))
    }
    pub fn accept(&mut self, revision: i64, ballot: Ballot, value: Value) -> Result<Receipt> {
        ensure!(
            revision == self.head.revision + 1 && ballot >= self.head.promised,
            "configuration revision superseded"
        );
        if let Some(previous) = &self.head.accepted {
            ensure!(
                previous.ballot != ballot || digest(&previous.value) == digest(&value),
                "ballot cannot accept two values"
            );
        }
        self.head.promised = ballot.clone();
        self.head.accepted = Some(Accepted {
            ballot: ballot.clone(),
            value: value.clone(),
        });
        self.save()?;
        let mut receipt = Receipt {
            node: self.node.clone(),
            revision,
            ballot,
            digest: digest(&value),
            signature: String::new(),
        };
        receipt.signature = sign(&self.key, &bytes(&receipt.message()));
        Ok(receipt)
    }
    pub fn commit(&mut self, revision: i64, value: Value, certificate: Vec<Receipt>) -> Result<()> {
        if revision <= self.head.revision {
            ensure!(
                revision != self.head.revision || digest(&value) == digest(&self.head.value),
                "conflicting checkpoint"
            );
            return Ok(());
        }
        let mut voters = BTreeSet::new();
        let mut ballots = BTreeSet::new();
        for receipt in &certificate {
            ensure!(
                self.members.contains(&receipt.node)
                    && receipt.revision == revision
                    && receipt.digest == digest(&value)
                    && verify(&self.key, &bytes(&receipt.message()), &receipt.signature),
                "invalid certificate"
            );
            voters.insert(receipt.node.clone());
            ballots.insert(receipt.ballot.clone());
        }
        ensure!(
            voters.len() >= 2 && ballots.len() == 1,
            "two independent acknowledgments required"
        );
        self.head.revision = revision;
        self.head.value = value;
        self.head.certificate = certificate;
        self.head.promised = (0, String::new());
        self.head.accepted = None;
        self.save()
    }
    pub fn applied(&mut self, revision: i64) -> Result<()> {
        if revision > self.head.revision {
            bail!("apply ahead of commit");
        }
        self.head.applied_revision = revision;
        self.save()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn journal(dir: &Path, node: &str) -> Journal {
        Journal::open(
            dir.join(node),
            [7; 32],
            node.into(),
            ["a", "b", "c"].map(String::from).into(),
            json!({"seed":true}),
        )
        .unwrap()
    }
    #[test]
    fn fsynced_promises_and_accepted_values_survive_restart() {
        let temp = tempfile::tempdir().unwrap();
        let mut a = journal(temp.path(), "a");
        a.promise(1, (2, "a".into())).unwrap();
        a.accept(1, (2, "a".into()), json!("chosen")).unwrap();
        drop(a);
        let mut a = journal(temp.path(), "a");
        assert!(a.promise(1, (1, "b".into())).is_err());
        assert_eq!(a.head.accepted.as_ref().unwrap().value, json!("chosen"));
        assert!(a.accept(1, (2, "a".into()), json!("different")).is_err());
    }
    #[test]
    fn certificates_require_two_distinct_members_and_bind_value_and_ballot() {
        let temp = tempfile::tempdir().unwrap();
        let mut a = journal(temp.path(), "a");
        let mut b = journal(temp.path(), "b");
        let value = json!("token-identity");
        let ballot = (1, "a".into());
        let ra = a.accept(1, ballot.clone(), value.clone()).unwrap();
        let rb = b.accept(1, ballot, value.clone()).unwrap();
        assert!(a
            .commit(1, value.clone(), vec![ra.clone(), ra.clone()])
            .is_err());
        assert!(a
            .commit(1, json!("forged"), vec![ra.clone(), rb.clone()])
            .is_err());
        let mut forged = rb.clone();
        forged.signature = "00".repeat(32);
        assert!(a
            .commit(1, value.clone(), vec![ra.clone(), forged])
            .is_err());
        a.commit(1, value.clone(), vec![ra.clone(), rb.clone()])
            .unwrap();
        let mut c = journal(temp.path(), "c");
        c.commit(1, value.clone(), vec![ra, rb]).unwrap();
        assert_eq!(c.head.value, value);
        assert_eq!(c.head.applied_revision, -1);
        assert!(c.commit(1, json!("conflict"), vec![]).is_err());
    }
    #[test]
    fn journal_is_encrypted_and_tampering_fails_closed() {
        let encrypted = encrypt(&[7; 32], b"private token identity").unwrap();
        assert!(!encrypted.windows(7).any(|w| w == b"private"));
        assert_eq!(
            decrypt(&[7; 32], &encrypted).unwrap(),
            b"private token identity"
        );
        let mut tampered = encrypted;
        tampered[15] ^= 1;
        assert!(decrypt(&[7; 32], &tampered).is_err());
    }
}
