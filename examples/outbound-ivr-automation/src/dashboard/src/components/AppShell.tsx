import { useEffect, useState } from "react"
import { NavLink, Outlet, useLocation } from "react-router-dom"
import { FileSearch, ListChecks, PhoneCall } from "lucide-react"
import { CommandPalette } from "@/components/CommandPalette"
import { Toaster } from "@/components/Toaster"
import { SidebarUserFooter } from "@/components/SidebarUserFooter"
import { ConnectSoftphoneProvider, useConnectSoftphoneContext } from "@/lib/connectSoftphoneContext"
import { getActiveCall, liveCallNavTarget, subscribeActiveCall } from "@/lib/activeCall"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarRail,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import { cn } from "@/lib/utils"

const NAV = [
  { to: "/", label: "Live call", icon: PhoneCall },
  { to: "/queue", label: "Case queue", icon: ListChecks },
  { to: "/evidence", label: "Evidence", icon: FileSearch },
]

export function AppShell() {
  // Keep the "Live call" link pointed at whichever call is active (or just ended, mid
  // after-call-work), so leaving that tab and clicking back in doesn't reset the page --
  // see lib/activeCall.ts and LiveCall.tsx, which together fix that bug.
  const [activeCall, setActiveCallState] = useState<string | null>(() => getActiveCall())
  useEffect(() => subscribeActiveCall(() => setActiveCallState(getActiveCall())), [])

  return (
    <ConnectSoftphoneProvider>
      <AppShellContent activeCall={activeCall} />
    </ConnectSoftphoneProvider>
  )
}

type AppShellContentProps = {
  activeCall: string | null
}

function AppShellContent({ activeCall }: AppShellContentProps) {
  // aws mode's Connect ringing indicator: a small badge on the "Live call" nav link so
  // an agent working another page (Case queue, Evidence) notices an incoming call and
  // goes to answer it -- the hidden CCP (ConnectSoftphoneProvider) has no sound of its
  // own, so without this a call ringing off this page was invisible until it rang out.
  const { phase } = useConnectSoftphoneContext()
  const { pathname } = useLocation()
  const ringingElsewhere = phase === "incoming" && pathname !== "/"

  return (
    <SidebarProvider>
        <Sidebar collapsible="icon">
          <SidebarHeader>
            <div className="flex items-center gap-2 px-2 py-1 group-data-[collapsible=icon]:px-0 group-data-[collapsible=icon]:justify-center">
              <span aria-hidden className="hidden text-sm font-bold group-data-[collapsible=icon]:block">A</span>
              <div className="grid group-data-[collapsible=icon]:hidden">
                <span className="text-sm font-bold leading-tight">Outbound IVR</span>
                <span className="text-[10px] leading-tight text-muted-foreground">
                  IVR Nova Sonic agent
                </span>
              </div>
            </div>
          </SidebarHeader>
          <SidebarContent>
            <SidebarGroup>
              <SidebarGroupContent>
                <SidebarMenu>
                  {NAV.map(({ to, label, icon: Icon }) => {
                    const target = to === "/" ? liveCallNavTarget(activeCall) : to
                    return (
                      <SidebarMenuItem key={to}>
                        <SidebarMenuButton asChild tooltip={label}>
                          <NavLink
                            to={target}
                            end={to === "/"}
                            className={({ isActive }) => cn(isActive && "bg-primary/10 text-primary")}
                          >
                            <Icon />
                            <span>{label}</span>
                            {to === "/" && ringingElsewhere && (
                              <span
                                aria-label="Incoming call"
                                title="Incoming call"
                                className="ml-auto h-2 w-2 animate-pulse rounded-full bg-destructive"
                              />
                            )}
                          </NavLink>
                        </SidebarMenuButton>
                      </SidebarMenuItem>
                    )
                  })}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          </SidebarContent>
          <SidebarFooter>
            <SidebarUserFooter />
          </SidebarFooter>
          <SidebarRail />
        </Sidebar>

        <SidebarInset>
          <div className="flex h-screen flex-col bg-background text-foreground">
            <header className="flex flex-none flex-wrap items-center gap-3 border-b border-border px-5 py-3 print:hidden">
              <SidebarTrigger />
              <div className="ml-auto flex items-center gap-2">
                <kbd
                  className="cursor-pointer rounded-md border border-border bg-secondary px-1.5 py-1 font-mono text-[11px] text-muted-foreground hover:text-foreground"
                  onClick={() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }))}
                >
                  ⌘K
                </kbd>
              </div>
            </header>
            <main className="min-h-0 flex-1 overflow-hidden">
              <Outlet />
            </main>
            <CommandPalette />
            <Toaster />
          </div>
        </SidebarInset>
    </SidebarProvider>
  )
}
