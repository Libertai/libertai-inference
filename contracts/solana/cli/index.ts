import { Command } from "commander";
import { InitializeCommand } from "./commands/initialize";
import { AddAdminCommand } from "./commands/addAdmin";
import { ProcessPaymentCommand } from "./commands/processPayment";
import { RemoveAdminCommand } from "./commands/removeAdmin";
import { ChangeOwnerCommand } from "./commands/changeOwner";
import { GetAdminsCommand } from "./commands/getAdmins";
import { WithdrawCommand } from "./commands/withdraw";
import { WithdrawSolCommand } from "./commands/withdraw_sol";
import * as os from "os";

export const program = new Command();
const homeDir = os.homedir();

program
  .name("libertai")
  .description("LibertAI Payment Processor CLI")
  .option("--payer-key-filepath <path>", "Path to payer private key JSON file", `${homeDir}/.config/solana/id.json`)
  .option("--payer-private-key <key>", "Payer private key as JSON string")
  .option("--json-rpc-endpoint <url>", "Solana RPC endpoint", "https://api.mainnet-beta.solana.com")
  .option("--amount <amount>", "Amount of tokens to transfer (in human-readable format, e.g. 60 for 60 tokens)")
  .option("--token-mint <address>", "Token mint address", "mntpN8z1d29f3MWhMD7VqZFpeYmbD88MgwS3Bkz8y7u")
  .option("--admin <address>", "Admin address")
  .option("--new-owner <address>", "New owner address")
  .option("--destination <address>", "Destination wallet address for withdrawal")

program
  .command("initialize")
  .description("Initialize the program")
  .action(InitializeCommand);

program
  .command("process-payment")
  .description("Process a payment and emit an event")
  .action(ProcessPaymentCommand);

program
  .command("add-admin")
  .description("Adds a new admin")
  .action(AddAdminCommand)

program
  .command("remove-admin")
  .description("Removes a new admin")
  .action(RemoveAdminCommand)

program
  .command("change-owner")
  .description("Change the owner of the program")
  .action(ChangeOwnerCommand)

program
  .command("get-admins")
  .description("Get the admins of the program")
  .action(GetAdminsCommand)

program
  .command("withdraw")
  .description("Withdraw LTAI tokens from program (admin/owner only)")
  .action(WithdrawCommand)

program
  .command("withdraw-sol")
  .description("Withdraw SOL from program (admin/owner only)")
  .action(WithdrawSolCommand)

program.parseAsync(process.argv);
