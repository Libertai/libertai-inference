import { Connection, Keypair, PublicKey, sendAndConfirmTransaction, Transaction } from "@solana/web3.js";
import { program } from "..";
import { getKeypair } from "../utils";
import { Program, BN, AnchorProvider } from "@coral-xyz/anchor";
import { LibertAiPaymentProcessor } from "../../target/types/libert_ai_payment_processor";
import idl from "../../target/idl/libert_ai_payment_processor.json";
import { getAssociatedTokenAddressSync, TOKEN_PROGRAM_ID, getMint } from "@solana/spl-token";

interface SolanaRpcResponse {
  jsonrpc: string;
  id: number;
  result?: {
    value?: {
      data?: {
        parsed?: {
          info?: {
            tokenAmount?: {
              uiAmount?: number;
            };
          };
        };
      };
    };
  };
  error?: any;
}

const getBalance = async (tokenMint: PublicKey, programId: PublicKey, networkURL: string): Promise<number> => {
  // Derive the program token account PDA (same as in withdraw function)
  const [programTokenAccount] = PublicKey.findProgramAddressSync(
    [Buffer.from("program_token_account"), tokenMint.toBuffer()],
    programId
  );
  
  const body = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "getAccountInfo",
    "params": [
      programTokenAccount.toBase58(),
      {
        "encoding": "jsonParsed",
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
    const json = await response.json() as SolanaRpcResponse;
    
    let balance = 0.0;
    if (json?.result?.value?.data?.parsed?.info?.tokenAmount?.uiAmount !== undefined) {
      balance = json.result.value.data.parsed.info.tokenAmount.uiAmount;
    }
    return balance;
  } catch (error) {
    console.error("Error fetching balance:", error);
    return 0;
  }
}


const waitForBalanceChange = async (
  tokenMint: PublicKey,
  programId: PublicKey,
  networkURL: string,
  expectedBalance: number,
  maxRetries: number = 10,
  retryDelayMs: number = 2000
): Promise<number> => {
  for (let i = 0; i < maxRetries; i++) {
    const currentBalance = await getBalance(tokenMint, programId, networkURL);
    if (currentBalance !== expectedBalance) {
      return currentBalance;
    }
    if (i < maxRetries - 1) {
      await new Promise(resolve => setTimeout(resolve, retryDelayMs));
    }
  }
  return await getBalance(tokenMint, programId, networkURL);
};

const withdraw = async (
  payer: Keypair,
  destinationWallet: PublicKey,
  amount: BN,
  tokenMint: PublicKey,
  program: Program,
  networkURL: string
) => {
  // Check if amount is zero
  if (amount.isZero()) {
    console.log("❌ Cannot withdraw 0 tokens. Please specify a valid amount.");
    return;
  }

  const destinationTokenAccount = getAssociatedTokenAddressSync(tokenMint, destinationWallet);
  
  const [programState] = PublicKey.findProgramAddressSync(
    [Buffer.from("program_state")],
    program.programId
  );
  
  const [programTokenAccount] = PublicKey.findProgramAddressSync(
    [Buffer.from("program_token_account"), tokenMint.toBuffer()],
    program.programId
  );
  
  const ix = await program.methods
    .withdraw(amount)
    .accounts({
      programState: programState,
      authority: payer.publicKey,
      programTokenAccount: programTokenAccount,
      destinationTokenAccount: destinationTokenAccount,
      tokenMint: tokenMint,
      tokenProgram: TOKEN_PROGRAM_ID,
    })
    .instruction();

  const balanceBefore = await getBalance(tokenMint, program.programId, networkURL)
  console.log(`Program balance before withdraw is ${balanceBefore}`)
  
  const tx = new Transaction().add(ix);
  const sig = await sendAndConfirmTransaction(program.provider.connection, tx, [payer]);
  
  console.log("Waiting for balance update...");
  const balanceAfter = await waitForBalanceChange(tokenMint, program.programId, networkURL, balanceBefore);
  console.log(`Program balance after withdraw is ${balanceAfter}`)
  console.log(`✅ Withdrew ${amount.toString()} tokens to ${destinationWallet.toString()}. Tx Signature: ${sig}`);
}

export const WithdrawCommand = async () => {
  const opts = program.opts();

  const payer = getKeypair({
    filepath: opts.payerKeyFilepath,
    key: opts.payerPrivateKey,
  });
  const connection = new Connection(opts.jsonRpcEndpoint, "confirmed");
  const wallet = {
    publicKey: payer.publicKey,
    signTransaction: async (tx: Transaction) => {
      tx.partialSign(payer);
      return tx;
    },
    signAllTransactions: async (txs: Transaction[]) => {
      txs.forEach(tx => tx.partialSign(payer));
      return txs;
    },
  };
  
  const anchorProgram = new Program(idl as LibertAiPaymentProcessor, {
    connection,
    publicKey: wallet.publicKey,
  });

  const destinationWallet = new PublicKey(opts.destination);
  const tokenMint = new PublicKey(opts.tokenMint);

  // Get token mint info to determine decimals
  const mintInfo = await getMint(connection, tokenMint);
  const decimals = mintInfo.decimals;
  
  // Convert amount from human-readable format to smallest units
  const humanAmount = parseFloat(opts.amount);
  const amount = new BN(humanAmount * Math.pow(10, decimals));

  console.log(`💰 Withdrawing ${humanAmount} tokens`);
  await withdraw(
    payer,
    destinationWallet,
    amount,
    tokenMint,
    anchorProgram,
      opts.jsonRpcEndpoint
  );
}