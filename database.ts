import { tool } from "@opencode-ai/plugin"
import path from "path"

import { getDatabaseConfig, listDatabaseProjects } from "./config.js"

const scriptPath = path.join(import.meta.dirname, "db", "exec.py")

type PythonToolInput = {
  query?: string
  operation?: "schema"
  config: {
    driver: string
    host: string
    port: number
    database: string
    user: string
    password: string
    sslmode: string
  }
}

function formatError(error: unknown): string {
  if (error instanceof Error) {
    return error.message
  }
  return String(error)
}

async function runPythonTool(input: PythonToolInput): Promise<string> {
  const localPython = path.join(import.meta.dirname, ".venv", "bin", "python")
  const python = await Bun.file(localPython).exists() ? localPython : "python3"
  const proc = Bun.spawn([python, scriptPath], {
    stdin: "pipe",
    stdout: "pipe",
    stderr: "pipe",
  })

  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    proc.kill("SIGKILL")
  }, 25000)

  try {
    proc.stdin.write(JSON.stringify(input))
    proc.stdin.end()
    const [output, , exitCode] = await Promise.all([
      new Response(proc.stdout).text(),
      new Response(proc.stderr).text(),
      proc.exited,
    ])
    if (timedOut) throw new Error("Database operation exceeded the 25 second time limit")
    if (exitCode !== 0) throw new Error("Database worker failed")
    if (new TextEncoder().encode(output.trimEnd()).byteLength > 1024 * 1024) {
      throw new Error("Database result exceeded the output limit")
    }
    return output.trimEnd()
  } finally {
    clearTimeout(timer)
    if (proc.exitCode === null) proc.kill("SIGKILL")
  }
}

export const database_exec = tool({
  description: "Execute a read-only Postgres SELECT query",
  args: {
    project: tool.schema.string().describe("Database project name from config.json"),
    environment: tool.schema.string().optional().describe("Database environment name"),
    query: tool.schema.string().describe("A single SQL SELECT query to execute"),
  },
  async execute(args) {
    if (!args.project?.trim()) {
      return "Не указан project"
    }

    try {
      const config = await getDatabaseConfig(args.project, args.environment)

      return await runPythonTool({
        query: args.query,
        config: {
          driver: config.driver,
          host: config.host,
          port: config.port,
          database: config.database,
          user: config.user,
          password: config.password,
          sslmode: config.sslmode,
        },
      })
    } catch (error) {
      return formatError(error)
    }
  },
})

export const database_schema = tool({
  description: "Read table and column metadata from the public application schema only. No row data or arbitrary SQL; at most 1000 columns.",
  args: {
    project: tool.schema.string().describe("Database project name from config.json"),
    environment: tool.schema.string().optional().describe("Database environment name"),
  },
  async execute(args) {
    if (!args.project?.trim()) return "Не указан project"
    try {
      const config = await getDatabaseConfig(args.project, args.environment)
      return await runPythonTool({
        operation: "schema",
        config: {
          driver: config.driver, host: config.host, port: config.port,
          database: config.database, user: config.user, password: config.password,
          sslmode: config.sslmode,
        },
      })
    } catch (error) {
      return formatError(error)
    }
  },
})

export const database_list = tool({
  description: "List available database projects and their environments",
  args: {},
  async execute() {
    try {
      const projects = await listDatabaseProjects()
      return JSON.stringify(projects, null, 2)
    } catch (error) {
      return formatError(error)
    }
  },
})
