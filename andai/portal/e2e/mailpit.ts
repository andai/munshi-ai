// Reads the stack's Mailpit (its HTTP API on 127.0.0.1) for the mails andai-core sends.

type MailSummary = { ID: string; Subject: string; Created: string };

export class Mailpit {
  constructor(private readonly base: string) {}

  private async messagesTo(to: string): Promise<MailSummary[]> {
    const response = await fetch(`${this.base}/api/v1/search?query=${encodeURIComponent(`to:"${to}"`)}`);
    if (!response.ok) return [];
    const { messages } = (await response.json()) as { messages: MailSummary[] };
    return messages.sort((a, b) => Date.parse(b.Created) - Date.parse(a.Created));
  }

  private async text(id: string): Promise<string> {
    const message = (await (await fetch(`${this.base}/api/v1/message/${id}`)).json()) as { Text: string };
    return message.Text;
  }

  /**
   * The portal link (`/invite?token=…` or `/reset-password?token=…`) in the newest mail to `to`
   * that carries one. Waits up to 30 s for it: some mails are sent after the response.
   * `skip` lists links already used, so a second mail of the same kind is found.
   */
  async link(to: string, route: "invite" | "reset-password", origin: string, skip: string[] = []): Promise<string> {
    const escaped = origin.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const pattern = new RegExp(`${escaped}/${route}\\?token=[A-Za-z0-9_\\-]+`);
    const deadline = Date.now() + 30_000;
    while (Date.now() < deadline) {
      for (const message of await this.messagesTo(to)) {
        const match = pattern.exec(await this.text(message.ID));
        if (match && !skip.includes(match[0])) return match[0];
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    throw new Error(`no ${route} link in mail to ${to} within 30 s`);
  }
}
