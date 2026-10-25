export function getAuthCookieName(): string {
  return process.env.MUNSHI_AUTH_COOKIE_NAME || "munshi-token";
}

export function getUserInfoCookieName(): string {
  return process.env.MUNSHI_USER_INFO_COOKIE_NAME || "munshi-user-info";
}
