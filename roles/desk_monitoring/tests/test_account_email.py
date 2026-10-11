import importlib.util
from pathlib import Path
import sqlite3
import unittest

spec=importlib.util.spec_from_file_location('account',Path(__file__).resolve().parents[1]/'files/account_email.py')
account=importlib.util.module_from_spec(spec);spec.loader.exec_module(account)

class AccountTests(unittest.TestCase):
    def database(self):
        db=sqlite3.connect(':memory:')
        db.execute('CREATE TABLE users (id TEXT PRIMARY KEY,email TEXT UNIQUE,role TEXT,password TEXT,tokenKey TEXT)')
        db.execute("INSERT INTO users VALUES ('same-id','admin@desk.home.arpa','admin','same-password-hash','same-token-key')")
        db.execute('CREATE TABLE system_stats (id TEXT,value TEXT)')
        db.execute("INSERT INTO system_stats VALUES ('history','retained')")
        db.commit();return db
    def test_rename_preserves_account_credentials_id_and_history_and_is_idempotent(self):
        with self.database() as db:
            self.assertTrue(account.rename(db,'admin@desk.cluster'))
            self.assertEqual(db.execute('select * from users').fetchone(),('same-id','admin@desk.cluster','admin','same-password-hash','same-token-key'))
            self.assertEqual(db.execute('select * from system_stats').fetchall(),[('history','retained')])
            self.assertFalse(account.rename(db,'admin@desk.cluster'))
    def test_conflicting_email_is_rejected_without_modification(self):
        with self.database() as db:
            db.execute("INSERT INTO users VALUES ('other','admin@desk.cluster','readonly','other-hash','other-key')");db.commit()
            with self.assertRaises(ValueError):account.rename(db,'admin@desk.cluster')
            self.assertEqual(db.execute("select email from users where id='same-id'").fetchone()[0],'admin@desk.home.arpa')
    def test_multiple_admins_are_rejected(self):
        with self.database() as db:
            db.execute("INSERT INTO users VALUES ('other','other@desk.cluster','admin','other-hash','other-key')");db.commit()
            with self.assertRaises(ValueError):account.rename(db,'admin@desk.cluster')

if __name__=='__main__':unittest.main()
