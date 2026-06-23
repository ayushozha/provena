/** Fixture for emit integration tests — search target: authenticate */
export function authenticate(user: string): boolean {
  return user.length > 0;
}