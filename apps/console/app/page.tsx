import { Suspense } from "react";
import { ConsoleDashboard } from "@/components/console-dashboard";

export default function Home() {
  return (
    <Suspense fallback={<div>Loading dashboard...</div>}>
      <ConsoleDashboard />
    </Suspense>
  );
}
