const field = id => document.getElementById(id);
const recoveryToken = new URLSearchParams(location.hash.slice(1)).get('token');
const changing = !recoveryToken && new URLSearchParams(location.search).get('mode') === 'change';
if (changing || recoveryToken) {field('mode-link').textContent = 'Forgot your password? Request recovery'; field('mode-link').href = '/reset-password';}
if (recoveryToken) history.replaceState(null, '', '/reset-password');
if (recoveryToken || changing) {
  field('heading').textContent = recoveryToken ? 'Set a new password' : 'Change password';
  field('help').textContent = 'This updates your Support Desk account password. Your existing sign-in sessions will end.';
  field('new-fields').hidden = false;
  field('next').required = field('confirm').required = true;
  field('employee-row').hidden = !!recoveryToken; field('employee').required = !recoveryToken;
  field('current-row').hidden = !changing; field('current').required = changing;
  field('submit').textContent = recoveryToken ? 'Reset password' : 'Change password';
}
field('reset-form').addEventListener('submit', async event => {
  event.preventDefault();
  if ((changing || recoveryToken) && field('next').value !== field('confirm').value) {field('result').textContent = 'New passwords do not match.'; return;}
  field('submit').disabled = true;
  const path = recoveryToken ? '/auth/reset-password' : changing ? '/auth/change-password' : '/auth/request-recovery';
  const body = recoveryToken ? {token:recoveryToken,new_password:field('next').value} : changing ? {employee_id:field('employee').value.trim(),password:field('current').value,new_password:field('next').value} : {employee_id:field('employee').value.trim()};
  try {
    const response = await fetch(path, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data = await response.json();
    field('result').textContent = response.ok ? data.message : typeof data.detail === 'string' ? data.detail : 'Check your inputs.';
    if(response.ok && (changing || recoveryToken)) { sessionStorage.removeItem('support-token'); ['current','next','confirm'].forEach(id => {field(id).value='';}); }
  } catch {field('result').textContent = 'Could not reach the server. Please try again.';}
  finally {field('submit').disabled = false;}
});