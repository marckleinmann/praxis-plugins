# Merlin, by Praxis

Merlin is an add-on for Claude (Claude calls it a plugin). It gives Claude a task list, notes about you and your business, and a few working habits, all kept in one folder on your own computer that you choose.

Your files and Merlin's code live in separate places. Updating or removing Merlin never touches your files.

## What you need

- A Mac with the Claude desktop app and a paid Claude plan (Pro or Max).
- About 20 minutes.
- No GitHub account and no Terminal.

## Install in the Claude app

1. In Finder, make a folder named after your business in your home folder, for example **Acme Tile**. This is your business folder: it holds what Merlin learns about you and the business, your task list, and a folder for each job or client. Setup suggests a folder with your business name later. To keep it somewhere else, such as Dropbox, make it there and tell setup where it is.
2. Open the Claude app, click the **Code** tab, and open your business folder.
3. Type `/plugin` in the chat box and press Return. The Plugins screen opens.
4. Add the Praxis catalog, which Claude calls a marketplace. Click **Add custom marketplace** (on some versions it is under **Manage marketplaces**) and paste the full address. It then shows up as **praxis-plugins**:

   ```
   https://github.com/marckleinmann/praxis-plugins
   ```

5. Find **Merlin** in the plugins list and click **Add** or **+**.
6. Start a new conversation in the Code tab, in your business folder.
7. Type `/merlin:setup`. Setup asks your business name and suggests a business folder named after it, such as `~/Acme Tile` in your home folder. Say yes, or tell it where you made the folder. It then copies a few starter files there and asks some short questions about you and your business.

No folder question appears when you add Merlin. That is expected: setup asks for it.

On claude.ai you can add the same catalog under **Customize**, **Plugins**, **Add**: choose to add a marketplace, pick the option that takes a web address, and paste the address above.

## Update

Open the Plugins screen (`/plugin`), refresh **praxis-plugins** under **Manage marketplaces**, then update **Merlin**. Start a new conversation afterwards.

## Remove

1. Type `/merlin:setup --remove`. It undoes what setup changed outside your business folder.
2. On the Plugins screen, remove **Merlin**, then remove **praxis-plugins** under **Manage marketplaces**.
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
