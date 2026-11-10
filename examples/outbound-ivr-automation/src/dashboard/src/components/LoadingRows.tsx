import { Skeleton } from "@/components/ui/skeleton"

/** Placeholder rows while a list loads from the API, so the page is not blank. */
export function LoadingRows({ label, rows = 5 }: { label: string; rows?: number }) {
  return (
    <div className="mt-4 space-y-2" role="status" aria-busy="true" aria-label={label}>
      <p className="text-xs text-muted-foreground">{label}</p>
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-9 w-full" />
      ))}
    </div>
  )
}
