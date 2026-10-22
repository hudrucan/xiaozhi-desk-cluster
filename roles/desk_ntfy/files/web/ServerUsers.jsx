import React, { useState } from "react";
import { Alert, Box, Button, Card, Chip, Container, Dialog, DialogActions, DialogContent, DialogTitle, Divider, List, ListItemButton, ListItemText, MenuItem, Stack, TextField, Typography } from "@mui/material";
import { serverUsersRequest } from "./ServerUsersApi";

const validName = (value) => /^[A-Za-z0-9_-]{1,64}$/.test(value);
const validPassword = (value) => new TextEncoder().encode(value).length >= 1 && new TextEncoder().encode(value).length <= 72;

export default function ServerUsers() {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [credential, setCredential] = useState(null);
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [issued, setIssued] = useState("");
  const [dialog, setDialog] = useState(null);
  const [name, setName] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [permission, setPermission] = useState("read");
  const [selectedName, setSelectedName] = useState(null);
  const selected = data?.users.find((user) => user.username === selectedName)
    || data?.users.find((user) => user.username === data.actor);

  const load = async (event) => {
    event?.preventDefault();
    const auth = credential || { username, password };
    setBusy(true); setError(""); setSaved(""); setIssued("");
    try {
      setData(await serverUsersRequest(auth));
      setCredential(auth); setPassword("");
    } catch (e) {
      setData(null); setError(e.message);
    } finally { setBusy(false); }
  };
  const open = (action, user, item) => {
    setDialog({ action, user, item }); setName(""); setNewPassword(""); setConfirmPassword(""); setPermission("read"); setIssued("");
  };
  const close = () => { setDialog(null); setNewPassword(""); setConfirmPassword(""); };
  const save = async (event) => {
    event.preventDefault();
    const { action, user, item } = dialog;
    const path = user ? `/${encodeURIComponent(user.username)}` : "";
    let suffix = path, method = "POST", body = {};
    if (action === "create") body = { username: name, password: newPassword };
    if (action === "password") { suffix += "/password"; method = "PUT"; body = { password: newPassword }; }
    if (action === "access") { suffix += "/access"; body = { topic: name, read: permission !== "write", write: permission !== "read" }; }
    if (action === "revoke-access") { suffix += `/access/${encodeURIComponent(item.topic)}`; method = "DELETE"; }
    if (action === "token") { suffix += "/token"; body = { label: name }; }
    if (action === "revoke-token") { suffix += `/token/${item.id}`; method = "DELETE"; }
    if (action === "delete") method = "DELETE";
    setBusy(true); setError(""); setSaved(""); setIssued("");
    try {
      const result = await serverUsersRequest(credential, suffix, method, body);
      const auth = action === "password" && user.username === data.actor
        ? { ...credential, password: newPassword } : credential;
      setCredential(auth);
      if (action === "create") setSelectedName(name);
      setSaved(`Saved revision ${result.revision}: at least 2/3 replicas committed and applied, including the receiving node.`);
      if (result.token) setIssued(result.token);
      close();
      try { setData(await serverUsersRequest(auth)); }
      catch (e) { setData(null); setError(`Save succeeded. Refresh failed: ${e.message}`); }
    } catch (e) {
      // An unsuccessful response is not a rollback. Require an explicit refresh, never replay a mutation.
      setData(null); setError(e.message); close();
      // Self-password Save may have committed even without an ACK: require login with the current password.
      if (action === "password" && user.username === data.actor) setCredential(null);
    } finally { setBusy(false); }
  };
  const action = dialog?.action;
  const passwordForm = action === "create" || action === "password";
  const valid = (!passwordForm || (validPassword(newPassword) && newPassword === confirmPassword))
    && (!(action === "create" || action === "access") || validName(name))
    && (action !== "token" || (name.length > 0 && name.length <= 256 && !/[\u0000-\u001f\u007f]/.test(name)));
  const titles = { create: "Create server user", password: "Change password", access: "Set topic access", "revoke-access": "Revoke topic access", token: "Issue bearer token", "revoke-token": "Revoke bearer token", delete: "Delete server user" };

  return <Container maxWidth="lg" sx={{ mt: 3, mb: 3 }}><Card sx={{ p: { xs: 2, sm: 3 } }} aria-label="Server users">
    <Stack spacing={2}>
      <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ justifyContent: "space-between", alignItems: { xs: "stretch", sm: "center" } }}>
        <Box><Typography variant="h5">Server users</Typography><Typography color="text.secondary" variant="body2" sx={{ mt: 0.5 }}>Accounts, topic permissions and access tokens.</Typography></Box>
        {data && <Button variant="contained" onClick={() => open("create")} disabled={busy}>Create server user</Button>}
      </Stack>
      {!credential ? <Box component="form" onSubmit={load}>
        <Stack spacing={2} sx={{ maxWidth: 420, py: 2 }}>
          <Typography color="text.secondary" variant="body2">Sign in as an administrator. Credentials stay in memory until you lock this page or leave it.</Typography>
          <TextField id="server-users-admin-name" label="Administrator username" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} disabled={busy} required />
          <TextField id="server-users-admin-password" label="Administrator password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} disabled={busy} required />
          <Button type="submit" variant="contained" disabled={busy || !username || !password}>Load server users</Button>
        </Stack>
      </Box> : <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ justifyContent: "space-between", alignItems: { xs: "flex-start", sm: "center" } }}>
        <Typography variant="body2" color="text.secondary">Signed in as {data?.actor || credential.username}{data ? ` · Revision ${data.revision}` : ""}</Typography>
        <Stack direction="row" spacing={1}>
          <Button size="small" aria-label="Refresh server users" onClick={load} disabled={busy}>Refresh</Button>
          <Button size="small" aria-label="Lock administration" onClick={() => { setCredential(null); setData(null); setIssued(""); setSaved(""); close(); }} disabled={busy}>Lock</Button>
        </Stack>
      </Stack>}
      {error && <Alert severity="error">{error}</Alert>}
      {saved && <Alert severity="success">{saved}</Alert>}
      {issued && <Alert severity="info">Copy this token now. Existing tokens are never displayed again.<Box component="code" sx={{ display: "block", overflowWrap: "anywhere", userSelect: "all" }}>{issued}</Box><Button onClick={() => setIssued("")}>Hide token</Button></Alert>}
      {data && selected && <>
        <Divider />
        <Stack direction={{ xs: "column", md: "row" }} spacing={3}>
          <Box sx={{ width: { xs: "100%", md: 240 }, flexShrink: 0 }}>
            <TextField id="server-users-selection" select fullWidth label="Select user" value={selected.username} onChange={(e) => setSelectedName(e.target.value)} disabled={busy} sx={{ display: { xs: "block", md: "none" } }}>
              {data.users.map((user) => <MenuItem key={user.username} value={user.username}>{user.username}</MenuItem>)}
            </TextField>
            <Typography variant="overline" color="text.secondary" sx={{ display: { xs: "none", md: "block" } }}>Users · {data.users.length}</Typography>
            <List disablePadding aria-label="Server account list" sx={{ display: { xs: "none", md: "block" } }}>
              {data.users.map((user) => <ListItemButton key={user.username} aria-label={`Manage ${user.username}`} selected={selected.username === user.username} disabled={busy} onClick={() => setSelectedName(user.username)} sx={{ borderRadius: 1, mb: 0.5 }}>
                <ListItemText primary={user.username} secondary={user.managed ? "Managed · Pi Events" : user.role === "admin" ? "Administrator" : "User"} slotProps={{ primary: { sx: { overflowWrap: "anywhere" } } }} />
              </ListItemButton>)}
            </List>
          </Box>
          <Box component="section" aria-label={`${selected.username} account`} sx={{ flex: 1, minWidth: 0 }}>
            <Stack spacing={3}>
              <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap" sx={{ alignItems: "center" }}><Typography variant="h6" sx={{ overflowWrap: "anywhere" }}>{selected.username}</Typography><Chip size="small" label={selected.role === "admin" ? "Administrator" : "User"} /></Stack>
              {selected.managed && <Alert severity="info">This account is managed by Pi Events and cannot be edited here.</Alert>}
              <Box>
                <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between" }}><Typography variant="subtitle1">Topic permissions</Typography><Button size="small" disabled={busy || selected.managed || selected.role === "admin"} onClick={() => open("access", selected)}>Set topic access</Button></Stack>
                {selected.role === "admin" ? <Typography variant="body2" color="text.secondary">Full access to all topics.</Typography>
                  : selected.access.length === 0 ? <Typography variant="body2" color="text.secondary">No topic access. For iPhone notifications, grant Read on desk-events.</Typography>
                    : selected.access.map((acl) => <Stack key={acl.topic} direction="row" spacing={1} sx={{ py: 1, alignItems: "center" }}>
                      <Typography variant="body2" sx={{ flex: 1, overflowWrap: "anywhere", minWidth: 0 }}>{acl.topic}</Typography>
                      <Chip size="small" label={acl.read && acl.write ? "Read + write" : acl.read ? "Read" : acl.write ? "Write" : "Denied"} />
                      <Button size="small" disabled={busy || selected.managed} onClick={() => open("revoke-access", selected, acl)}>Revoke access</Button>
                    </Stack>)}
              </Box>
              <Box>
                <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between" }}><Typography variant="subtitle1">Access tokens</Typography><Button size="small" disabled={busy || selected.managed} onClick={() => open("token", selected)}>Issue token</Button></Stack>
                {selected.tokens.length === 0 && <Typography variant="body2" color="text.secondary">No tokens issued.</Typography>}
                {selected.tokens.map((token) => <Stack key={token.id} direction="row" spacing={1} sx={{ py: 1, alignItems: "center" }}>
                  <Box sx={{ flex: 1, minWidth: 0 }}><Typography variant="body2" sx={{ overflowWrap: "anywhere" }}>{token.label || "Unnamed"}</Typography><Typography variant="caption" color="text.secondary">{token.expires === null ? "No expiry" : new Date(token.expires * 1000).toLocaleString()}</Typography></Box>
                  <Button size="small" disabled={busy || selected.managed} onClick={() => open("revoke-token", selected, token)}>Revoke token</Button>
                </Stack>)}
              </Box>
              <Divider />
              <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap" sx={{ justifyContent: "space-between" }}>
                <Button variant="outlined" disabled={busy || selected.managed} onClick={() => open("password", selected)}>Change password</Button>
                <Button color="error" disabled={busy || selected.managed || selected.username === data.actor} onClick={() => open("delete", selected)}>Delete user</Button>
              </Stack>
            </Stack>
          </Box>
        </Stack>
      </>}
    </Stack>
    <Dialog open={!!dialog} onClose={busy ? undefined : close} fullWidth maxWidth="sm">
      <Box component="form" onSubmit={save}>
        <DialogTitle>{titles[action]}{dialog?.user ? ` · ${dialog.user.username}` : ""}</DialogTitle>
        <DialogContent><Stack spacing={2} sx={{ pt: 1 }}>
          {(action === "create" || action === "access" || action === "token") && <TextField id="server-users-new-name" label={action === "create" ? "Username" : action === "access" ? "Topic" : "Token label"} value={name} onChange={(e) => setName(e.target.value)} disabled={busy} required autoFocus />}
          {passwordForm && <>
            <TextField id="server-users-new-password" label="New password" type="password" autoComplete="new-password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} disabled={busy} required helperText="1–72 UTF-8 bytes" />
            <TextField id="server-users-confirm-password" label="Confirm new password" type="password" autoComplete="new-password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} disabled={busy} required />
            {action === "password" && <Typography>Other devices using this password must update their saved credentials. Existing tokens and topic permissions are preserved.</Typography>}
          </>}
          {action === "access" && <TextField id="server-users-permission" select label="Permission" value={permission} onChange={(e) => setPermission(e.target.value)} disabled={busy}>
            <MenuItem value="read">Read only</MenuItem><MenuItem value="write">Write only</MenuItem><MenuItem value="both">Read and write</MenuItem>
          </TextField>}
          {action?.startsWith("revoke") && <Typography>Revoke {dialog.item.topic || dialog.item.label || "this token"}?</Typography>}
          {action === "delete" && <Typography>Delete this user and revoke all their tokens and topic access?</Typography>}
        </Stack></DialogContent>
        <DialogActions><Button onClick={close} disabled={busy}>Cancel</Button><Button type="submit" disabled={busy || !valid}>Save to cluster</Button></DialogActions>
      </Box>
    </Dialog>
  </Card></Container>;
}
