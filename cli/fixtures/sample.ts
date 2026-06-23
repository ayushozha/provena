/** Module-level documentation for the sample service. */
import { createHash } from "node:crypto";
import type { User } from "./types";

/** Authenticate a user by token. */
export function authenticate(token: string): User | null {
  return null;
}

/** In-memory user store. */
export class UserStore {
  /** Find a user by id. */
  findById(id: string): User | null {
    return null;
  }
}

/** Public user profile shape. */
export interface UserProfile {
  id: string;
  name: string;
}

/** Application configuration alias. */
export type AppConfig = {
  debug: boolean;
};