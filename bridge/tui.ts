import { Plugin } from "@opencode/plugin/tui"
import { readFile, writeFile, rename, mkdir } from "node:fs/promises"
import path from "node:path"

// Only the managed viewer receives these env vars. No listener, credentials or global config.
export default Plugin.define({
  id: "codex.delegate.bridge",
  setup(context) {
    const directory = process.env.OC_DELEGATE_BRIDGE
    const token = process.env.OC_DELEGATE_BRIDGE_TOKEN
    const workspace = process.env.OC_DELEGATE_BRIDGE_WORKSPACE
    if (!directory || !token || !workspace) return
    let stopped = false
    let busy = false
    let lastID = ""
    let lastHeartbeat = 0
    const normalize = (value: string) => {
      const resolved = path.resolve(value)
      return process.platform === "win32" ? resolved.toLowerCase() : resolved
    }
    async function atomic(name: string, value: unknown) {
      const target = path.join(directory!, name)
      const temp = target + "." + process.pid + ".tmp"
      await writeFile(temp, JSON.stringify(value), "utf8")
      for (let attempt = 0; ; attempt++) {
        try { await rename(temp, target); break }
        catch (error: any) {
          if (attempt >= 50 || !["EPERM", "EACCES", "EBUSY"].includes(error?.code)) throw error
          await new Promise((resolve) => setTimeout(resolve, 20))
        }
      }
    }
    function activeSession() {
      const tabs = context.ui.tabs
      if (tabs.enabled()) return tabs.list().find((tab) => tab.active)?.sessionID
      return context.ui.router.current()?.sessionID
    }
    async function tick() {
      if (busy || stopped) return
      busy = true
      try {
        await mkdir(directory!, { recursive: true })
        if (Date.now() - lastHeartbeat >= 2000) {
          await atomic("ready.json", { token, pid: process.pid, workspace, version: 1,
            active_session: activeSession(), tabs: context.ui.tabs.list(), heartbeat_at: Date.now() })
          lastHeartbeat = Date.now()
        }
        let request
        try { request = JSON.parse(await readFile(path.join(directory!, "request.json"), "utf8")) }
        catch { return }
        if (request.token !== token || request.id === lastID || typeof request.id !== "string") return
        if (normalize(request.workspace ?? "") !== normalize(workspace!)) return
        lastID = request.id
        try {
          if (!/^ses_[A-Za-z0-9]+$/.test(request.session_id)) throw new Error("Invalid session id")
          const currentLocation = context.location ?? context.data.location.default()
          if (currentLocation?.directory && normalize(currentLocation.directory) !== normalize(workspace!)) {
            throw new Error("Viewer moved to another workspace; request rejected")
          }
          if (request.action === "close_tab") {
            context.ui.tabs.close(request.session_id)
            for (let i = 0; i < 30 && !stopped; i++) {
              if (!context.ui.tabs.list().some((tab) => tab.sessionID === request.session_id)) break
              await new Promise((resolve) => setTimeout(resolve, 50))
            }
            const closed = !context.ui.tabs.list().some((tab) => tab.sessionID === request.session_id)
            await atomic("reply.json", { id: request.id, token, pid: process.pid, ok: closed,
              closed, active_session: activeSession(), tabs: context.ui.tabs.list() })
            return
          }
          if (request.action && request.action !== "select_tab") throw new Error("Unknown bridge action")
          await context.data.session.sync(request.session_id)
          if (!context.data.session.get(request.session_id)) throw new Error("Session not available in viewer")
          if (context.ui.tabs.enabled()) context.ui.tabs.focus(request.session_id)
          else context.ui.router.navigate({ type: "session", sessionID: request.session_id })
          // Solid navigation may settle on the next tick; acknowledge observed state, not the call.
          for (let i = 0; i < 30 && !stopped; i++) {
            if (activeSession() === request.session_id) break
            await new Promise((resolve) => setTimeout(resolve, 50))
          }
          const selected = activeSession()
          await atomic("reply.json", { id: request.id, token, pid: process.pid,
            ok: selected === request.session_id, active_session: selected,
            tabs: context.ui.tabs.list(), error: selected === request.session_id ? null : "Tab did not become active" })
        } catch (error) {
          await atomic("reply.json", { id: request.id, token, pid: process.pid, ok: false,
            error: String(error), active_session: activeSession() })
        }
      } catch (error) {
        // stderr is the diagnostic channel; never write to the MCP wire.
        console.error("codex.delegate.bridge:", String(error))
      } finally { busy = false }
    }
    const timer = setInterval(() => { void tick() }, 200)
    void tick()
    return () => { stopped = true; clearInterval(timer) }
  },
})
