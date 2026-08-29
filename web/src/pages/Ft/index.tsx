import { useFt } from "@/pages/Ft/useFt";
import { FtPage } from "@/pages/Ft/FtPage";

export function FtRoute() {
  return <FtPage {...useFt()} />;
}
