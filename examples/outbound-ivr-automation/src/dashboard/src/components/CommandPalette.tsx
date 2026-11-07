import { useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"
import { FileSearch, ListChecks, Monitor, Moon, PhoneCall, Sun } from "lucide-react"
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command"
import { useTheme } from "@/lib/theme"

export function CommandPalette() {
  const [open, setOpen] = useState(false)
  const navigate = useNavigate()
  const { setTheme } = useTheme()

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault()
        setOpen((v) => !v)
      }
    }
    document.addEventListener("keydown", handler)
    return () => document.removeEventListener("keydown", handler)
  }, [])

  const run = (fn: () => void) => {
    setOpen(false)
    fn()
  }

  return (
    <CommandDialog open={open} onOpenChange={setOpen} title="Command palette" description="Jump around Outbound IVR">
      <CommandInput placeholder="Type a command…" />
      <CommandList>
        <CommandEmpty>No matches.</CommandEmpty>
        <CommandGroup heading="Navigate">
          <CommandItem onSelect={() => run(() => navigate("/"))}>
            <PhoneCall /> Live call
          </CommandItem>
          {/* "Start call" has no home here: starting a call needs a case, and the case
              queue page (which has one per row -- see pages/CaseQueue.tsx) is one click
              away. A palette command with no selected case to act on would either post
              a malformed start-call request or need to invent a case to act on. */}
          <CommandItem onSelect={() => run(() => navigate("/queue"))}>
            <ListChecks /> Case queue
          </CommandItem>
          <CommandItem onSelect={() => run(() => navigate("/evidence"))}>
            <FileSearch /> Evidence
          </CommandItem>
        </CommandGroup>
        <CommandSeparator />
        <CommandGroup heading="Theme">
          <CommandItem onSelect={() => run(() => setTheme("light"))}>
            <Sun /> Theme: light
          </CommandItem>
          <CommandItem onSelect={() => run(() => setTheme("dark"))}>
            <Moon /> Theme: dark
          </CommandItem>
          <CommandItem onSelect={() => run(() => setTheme("system"))}>
            <Monitor /> Theme: system
          </CommandItem>
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  )
}
