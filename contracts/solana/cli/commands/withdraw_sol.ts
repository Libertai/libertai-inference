import { AnchorProvider, BN, Program, Wallet } from "@coral-xyz/anchor";
import { Connection, Keypair, LAMPORTS_PER_SOL, PublicKey } from "@solana/web3.js";
import { program } from "..";
import idl from "../../target/idl/libertai_payment_processor.json";
import { LibertaiPaymentProcessor } from "../../target/types/libertai_payment_processor";
import { getKeypair } from "../utils";

const getSolBalance = async (programId: PublicKey, networkURL: string): Promise<number> => {
  // Check both program_state and program account balances
  const [programState] = PublicKey.findProgramAddressSync(
    [Buffer.from("program_state")],
    programId
  );

  const body = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "getMultipleAccounts",
    "params": [
      [programState.toBase58(), programId.toBase58()],
      {
        "encoding": "base64",
      }
    ],
  };

  try {
    const response = await fetch(networkURL, {
      method: "POST",
      body: JSON.stringify(body),
      headers: {
        "Content-Type": "application/json"
      }
    });
    const json = await response.json() as any;

    let programStateBalance = 0;
    let programAccountBalance = 0;

    if (json?.result?.value?.[0]?.lamports !== undefined) {
      programStateBalance = json.result.value[0].lamports / LAMPORTS_PER_SOL;
    }
    if (json?.result?.value?.[1]?.lamports !== undefined) {
      programAccountBalance = json.result.value[1].lamports / LAMPORTS_PER_SOL;
    }

    return programStateBalance;
  } catch (error) {
    console.error("Error fetching SOL balance:", error);
    return 0;
  }
}

const waitForSolBalanceChange = async (
  programId: PublicKey,
  networkURL: string,
  expectedBalance: number,
  maxRetries: number = 10,
  retryDelayMs: number = 5000
): Promise<number> => {
  for (let i = 0; i < maxRetries; i++) {
    const currentBalance = await getSolBalance(programId, networkURL);
    if (currentBalance !== expectedBalance) {
      return currentBalance;
    }
    if (i < maxRetries - 1) {
      await new Promise(resolve => setTimeout(resolve, retryDelayMs));
    }
  }
  return await getSolBalance(programId, networkURL);
};

const withdrawSol = async (
  payer: Keypair,
  destinationWallet: PublicKey,
  amount: BN,
  program: Program,
  networkURL: string
) => {
  // Check if amount is zero
  if (amount.isZero()) {
    console.log("❌ Cannot withdraw 0 SOL. Please specify a valid amount.");
    return;
  }

  const [programState] = PublicKey.findProgramAddressSync(
    [Buffer.from("program_state")],
    program.programId
  );

  const balanceBefore = await getSolBalance(program.programId, networkURL);
  console.log(`Program SOL balance before withdraw: ${balanceBefore} SOL`);

  const sig = await program.methods
    .withdrawSol(amount)
    .accounts({
      programState: programState,
      authority: payer.publicKey,
      destination: destinationWallet,
    })
    .signers([payer])
    .rpc();

  console.log("Waiting for balance update...");
  const balanceAfter = await waitForSolBalanceChange(program.programId, networkURL, balanceBefore);
  console.log(`Program SOL balance after withdraw: ${balanceAfter} SOL`);
  console.log(`✅ Withdrew ${amount.toNumber() / LAMPORTS_PER_SOL} SOL to ${destinationWallet.toString()}. Tx Signature: ${sig}`);
}

export const WithdrawSolCommand = async () => {
  const opts = program.opts();

  const payer = getKeypair({
    filepath: opts.payerKeyFilepath,
    key: opts.payerPrivateKey,
  });
  const connection = new Connection(opts.jsonRpcEndpoint, "confirmed");

  const wallet = new Wallet(payer);
  const provider = new AnchorProvider(connection, wallet, {});
  const anchorProgram = new Program(idl as LibertaiPaymentProcessor, provider);

  const destinationWallet = new PublicKey(opts.destination);

  // Convert amount from SOL to lamports
  const humanAmount = parseFloat(opts.amount);
  const amount = new BN(humanAmount * LAMPORTS_PER_SOL);

  console.log(`💰 Withdrawing ${humanAmount} SOL`);

  await withdrawSol(
    payer,
    destinationWallet,
    amount,
    anchorProgram,
    opts.jsonRpcEndpoint
  );
}
