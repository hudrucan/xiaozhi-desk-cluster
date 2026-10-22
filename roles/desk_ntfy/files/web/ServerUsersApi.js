// Administration uses the same-origin Rust quorum boundary, never native auth writes.
export async function serverUsersRequest(credential, path = "", method = "GET", body) {
  const raw = new TextEncoder().encode(`${credential.username}:${credential.password}`);
  const authorization = `Basic ${btoa(Array.from(raw, (byte) => String.fromCharCode(byte)).join(""))}`;
  let response;
  try {
    response = await fetch(`/v1/admin/users${path}`, {
      method,
      headers: { Authorization: authorization, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
      signal: AbortSignal.timeout(90000),
    });
  } catch {
    throw new Error(method === "GET"
      ? "Cannot reach the auth quorum. Check connectivity and refresh."
      : "Save was not acknowledged. It may have applied; refresh before retrying. Do not automatically issue another token.");
  }
  if (!response.ok) {
    const errors = {
      400: "Invalid change. Check the fields; keep at least one administrator and leave the managed Pi account unchanged.",
      401: "Incorrect or expired administrator credentials.",
      403: "Administrator access from this server's own UI is required.",
    };
    throw new Error(errors[response.status] || "Quorum did not acknowledge the request. Refresh before retrying; a Save may already have applied.");
  }
  try {
    const result = await response.json();
    if (!Number.isSafeInteger(result.revision) || result.revision < 0
      || (method === "GET" && (!Array.isArray(result.users) || typeof result.actor !== "string"))) {
      throw new Error("invalid response");
    }
    return result;
  } catch {
    throw new Error("Invalid acknowledgement. Refresh before retrying; a Save may already have applied.");
  }
}
