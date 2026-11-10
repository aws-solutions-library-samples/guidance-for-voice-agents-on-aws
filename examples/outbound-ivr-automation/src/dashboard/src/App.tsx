import { Route, Routes } from "react-router-dom"
import { AppShell } from "@/components/AppShell"
import { LiveCall } from "@/pages/LiveCall"
import { EvidenceList } from "@/pages/EvidenceList"
import { EvidenceDetail } from "@/pages/EvidenceDetail"
import { CaseQueue } from "@/pages/CaseQueue"
import { AuthCallback } from "@/pages/AuthCallback"
import { AuthGate } from "@/lib/AuthGate"

function Shell() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route path="/" element={<LiveCall />} />
        <Route path="/queue" element={<CaseQueue />} />
        <Route path="/evidence" element={<EvidenceList />} />
        <Route path="/evidence/:id" element={<EvidenceDetail />} />
      </Route>
    </Routes>
  )
}

function App() {
  return (
    <Routes>
      {/* The Cognito Hosted UI's redirect_uri lands here, outside AuthGate (there's no
          user yet when it does). */}
      <Route path="/auth/callback" element={<AuthCallback />} />
      <Route
        path="/*"
        element={
          <AuthGate>
            <Shell />
          </AuthGate>
        }
      />
    </Routes>
  )
}

export default App
