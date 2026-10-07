# Merlin, by Praxis

Merlin is an add-on for Claude (Claude calls it a plugin). It gives Claude a task list, notes about you and your business, and a few working habits, all kept in one folder on your own computer that you choose.

Your files and Merlin's code live in separate places. Updating or removing Merlin never touches your files.

## What you need

- A Mac with the Claude desktop app and a paid Claude plan (Pro or Max).
- About 20 minutes.
- No GitHub account and no Terminal.

## Install in the Claude app

1. Make your business folder. We recommend Dropbox, so it is backed up: in Finder, click **Dropbox** in the sidebar, choose **File > New Folder**, and name it after your business, for example **Acme Tile**. No Dropbox? Make it in **Documents** instead. This folder holds what Merlin learns about you and the business, your task list, and a folder for each job or client. Once it is made, do not move or rename it. If Praxis sent you a folder already set up for your business, use that one instead.
2. Open the Claude app, click the **Code** tab, and open your business folder. Keep the Code tab open on it for the rest of the install.
3. Click your name (your profile), then **Settings**. Scroll down to the **Customize** section and click **Plugins**.
4. Add the Praxis catalog, which Claude calls a marketplace. Click **Add**, then **Add custom marketplace**, then **Add from repository**. Paste the full address and click **Sync**. If the app offers to connect GitHub, skip it: Merlin does not need a GitHub account.

   ```
   https://github.com/marckleinmann/praxis-plugins
   ```

5. Next to **Merlin**, click **Add**.
6. Go back to the Code tab, still open on your business folder, and start a new conversation.
7. Type `/merlin:setup`. Setup offers the folder you have open as your business folder. Say yes. It copies a few starter files there, asks some short questions about you and your business, and offers to set up your first project. In a folder Praxis prepared, it reads back what is already there and asks only about what is missing.

Use the business folder from one Mac at a time: two Macs changing it at once can leave "conflicted copy" files.

No folder question appears when you add Merlin. That is expected: setup asks for it.

On claude.ai you can add the same catalog under **Customize**, **Plugins**, **Add**: choose to add a marketplace, pick the option that takes a web address, and paste the address above.

## Update

Click your name, then **Settings**, and scroll down to **Plugins**. Click **Sync** on **praxis-plugins**, then update **Merlin**. Start a new conversation afterwards.

## Remove

1. Type `/merlin:setup --remove`. It undoes what setup changed outside your business folder.
2. In **Settings**, **Plugins**, remove **Merlin**, then remove the **praxis-plugins** catalog.
3. If you also added Merlin on claude.ai, remove it there too. Removing it in one place does not remove it in the other.

Your business folder and every file in it stay where they are.

## Terminal route (developers only)

```
claude plugin marketplace add https://github.com/marckleinmann/praxis-plugins
claude plugin install merlin@praxis-plugins
```

Then run `/merlin:setup` in a Claude Code session. A terminal install can also set the plugin option "Your business folder"; when it is set, it wins over the folder setup records.

## Licence

See `LICENSE` and `NOTICE` in this repository.
