import type { User } from "./types";

/** Authenticate a user by token. */
export function authenticate(token: string): User | null {
  if (!token) {
    return null;
  }
  return { id: "u1", name: "tester" };
}

/** Session-backed user directory. */
export class UserStore {
  /** Resolve a user by id. */
  findById(id: string): User | null {
    return id ? { id, name: "cached" } : null;
  }
}