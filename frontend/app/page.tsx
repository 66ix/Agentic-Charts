import ChartWorkspace from "@/components/ChartWorkspace";
import MarketHeader from "@/components/MarketHeader";

export default function Page() {
  return (
    <div className="flex h-dvh flex-col overflow-hidden">
      <MarketHeader />
      <ChartWorkspace />
    </div>
  );
}
