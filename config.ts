import { readFile } from "node:fs/promises"
import { fileURLToPath } from "node:url"

const configDir = fileURLToPath(new URL(".", import.meta.url))
const CONFIG_PATH = `${configDir}/config.json`

export interface DatabaseConfig {
  driver: string
  host: string
  port: number
  database: string
  sslmode: string
  auth: {
    user: string
    password: string
  }
}

export interface ConfigData {
  projects: Record<string, Record<string, DatabaseConfig>>
}

let _cache: ConfigData | null = null

export async function readConfig(): Promise<ConfigData> {
  if (_cache) return _cache
  const raw = await readFile(CONFIG_PATH, "utf-8")
  _cache = JSON.parse(raw) as ConfigData
  return _cache!
}

export function clearCache(): void {
  _cache = null
}

export async function getDatabaseConfig(
  project: string,
  environment?: string
): Promise<Omit<DatabaseConfig, "auth"> & { user: string; password: string }> {
  const config = await readConfig()
  const db = config.projects[project]
  if (!db) {
    const available = Object.keys(config.projects).join(", ")
    throw new Error(`Проект '${project}' не найден. Доступные: ${available}`)
  }
  const env = environment ?? Object.keys(db)[0]
  if (!env) {
    throw new Error(`У проекта '${project}' нет ни одного окружения`)
  }
  const entry = db[env]
  if (!entry) {
    const available = Object.keys(db).join(", ")
    throw new Error(
      `Окружение '${environment}' не найдено в проекте '${project}'. Доступные окружения: ${available}`
    )
  }
  return {
    driver: entry.driver,
    host: entry.host,
    port: entry.port,
    database: entry.database,
    sslmode: entry.sslmode,
    user: entry.auth.user,
    password: entry.auth.password,
  }
}

export async function listDatabaseProjects(): Promise<Record<string, string[]>> {
  const config = await readConfig()
  const result: Record<string, string[]> = {}
  for (const [project, envs] of Object.entries(config.projects)) {
    result[project] = Object.keys(envs)
  }
  return result
}
