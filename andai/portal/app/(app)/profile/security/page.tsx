import type { Metadata } from "next";

import { ProfileSecurity } from "@/components/app/profile-security";

export const metadata: Metadata = { title: "Security settings" };

export default function ProfileSecurityPage() {
  return <ProfileSecurity />;
}
