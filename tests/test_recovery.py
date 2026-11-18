import os

# Never depend on (or talk to) whatever model the developer configured in their shell: these tests use the
# deterministic playbook engine, and the model-loop tests install scripted models and switch the mode themselves.
os.environ["AGENT_MODE"] = "playbook"
os.environ["AGENT_PREFETCH"] = "1"
for _name in ("LLM_PROVIDER", "AGENT_MODEL", "OLLAMA_MODEL"):
    os.environ.pop(_name, None)

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import database
from auth import create_employee, token_digest
from main import app


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patcher = patch.object(database, 'DATABASE_PATH', Path(self.tmp.name) / 'test.db')
        self.patcher.start()
        database.initialize_database()
        for name, role in [('alice','employee'),('bob','employee'),('support','it_support')]:
            create_employee(name, name, 'Original-password!', role)
        self.client = TestClient(app)
        self.headers = {}
        for name in ['alice','bob','support']:
            token = self.client.post('/auth/login',json={'employee_id':name,'password':'Original-password!'}).json()['access_token']
            self.headers[name]={'Authorization':'Bearer '+token}

    def tearDown(self):
        self.client.close()
        self.patcher.stop()
        self.tmp.cleanup()

    def issue(self):
        response = self.client.post('/auth/reset-link',headers=self.headers['support'],json={'employee_id':'alice','identity_verified':True})
        self.assertEqual(response.status_code,200)
        return response.json()['reset_path'].split('token=')[1]

    def test_reset_is_single_use_and_revokes_sessions(self):
        token = self.issue()
        body={'token':token,'new_password':'Replacement-password!'}
        self.assertEqual(self.client.post('/auth/reset-password',json=body).status_code,200)
        self.assertEqual(self.client.post('/auth/reset-password',json=body).status_code,400)
        self.assertEqual(self.client.get('/me',headers=self.headers['alice']).status_code,401)
        self.assertEqual(self.client.post('/auth/login',json={'employee_id':'alice','password':'Original-password!'}).status_code,401)
        self.assertEqual(self.client.post('/auth/login',json={'employee_id':'alice','password':'Replacement-password!'}).status_code,200)

    def test_employee_cannot_issue_link(self):
        self.assertEqual(self.client.post('/auth/reset-link',headers=self.headers['alice'],json={'employee_id':'bob','identity_verified':True}).status_code,403)
        self.assertEqual(self.client.post('/auth/reset-link',json={'employee_id':'alice','identity_verified':True}).status_code,401)
        self.assertEqual(self.client.post('/auth/reset-link',headers=self.headers['support'],json={'employee_id':'alice','identity_verified':False}).status_code,422)

    def test_expired_link_and_bad_password_rejected(self):
        token=self.issue()
        with database.connect() as db:
            db.execute('UPDATE password_resets SET expires_at=0 WHERE token_hash=?',(token_digest(token),))
        self.assertEqual(self.client.post('/auth/reset-password',json={'token':token,'new_password':'Replacement-password!'}).status_code,400)
        self.assertEqual(self.client.post('/auth/change-password',json={'employee_id':'alice','password':'Wrong-password!','new_password':'Replacement-password!'}).status_code,401)

    def test_change_password_invalidates_reset_link(self):
        token=self.issue()
        response=self.client.post('/auth/change-password',json={'employee_id':'alice','password':'Original-password!','new_password':'Replacement-password!'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.post('/auth/reset-password',json={'token':token,'new_password':'Another-password!'}).status_code,400)
        self.assertEqual(self.client.get('/reset-password').status_code,200)

    def test_support_opens_employee_ticket_and_changes_status(self):
        result=self.client.post('/chat',headers=self.headers['alice'],json={'message':'My laptop screen is cracked.'}).json()
        ticket_id=result['created_tickets'][0]['id']
        path=f'/tickets/{ticket_id}'
        self.assertEqual(self.client.get(path,headers=self.headers['support']).status_code,200)
        self.assertEqual(self.client.get(path,headers=self.headers['bob']).status_code,404)
        self.assertEqual(self.client.patch(path,headers=self.headers['alice'],json={'status':'resolved'}).status_code,403)
        for status in ['in_progress','resolved']:
            response=self.client.patch(path,headers=self.headers['support'],json={'status':status})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['status'],status)

    def test_recovery_request_is_private_and_deduplicated(self):
        body={'employee_id':'alice'}
        first=self.client.post('/auth/request-recovery',json=body)
        second=self.client.post('/auth/request-recovery',json=body)
        unknown=self.client.post('/auth/request-recovery',json={'employee_id':'nonexistent'})
        self.assertEqual(first.status_code,200)
        self.assertEqual(first.json(),unknown.json())
        self.assertEqual(first.json(),second.json())
        tickets=self.client.get('/tickets',headers=self.headers['support']).json()
        self.assertEqual(len(tickets),1)
        self.assertEqual(tickets[0]['employee_id'],'alice')
        self.assertIn('Unauthenticated',tickets[0]['description'])
