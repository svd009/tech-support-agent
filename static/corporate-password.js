/* One-time corporate password reset. The token stays in the URL fragment (never sent in a URL). */
const field = id => document.getElementById(id);
const token = new URLSearchParams(location.hash.slice(1)).get('token');
if (token) history.replaceState(null, '', '/corporate-password');
if (!token) { field('no-token').hidden = false; field('fields').hidden = true; }
field('reset-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (field('next').value !== field('confirm').value) { field('result').textContent = 'The passwords do not match.'; return; }
  field('submit').disabled = true;
  try {
    const response = await fetch('/auth/corporate-password/reset', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token, new_password: field('next').value})});
    const data = await response.json();
    field('result').textContent = response.ok ? data.message : (typeof data.detail === 'string' ? data.detail : 'Check the password requirements.');
    if (response.ok) { field('fields').hidden = true; }
  } catch { field('result').textContent = 'Could not reach the server. Please try again.'; }
  finally { field('submit').disabled = false; field('next').value = ''; field('confirm').value = ''; }
});
